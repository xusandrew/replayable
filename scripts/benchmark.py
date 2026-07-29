#!/usr/bin/env python3
"""Calculate recorded token cost and compare record/replay wall time."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from replayable.cassette import CassetteReader, sse_chunk_bytes

PRICING_SOURCE = "https://docs.anthropic.com/en/docs/about-claude/pricing"
PRICING_VERIFIED = "2026-07-18"
MODEL_PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
# Anthropic prices prompt-cache writes at 1.25x and cache reads at 0.1x the
# input rate for the models above.
CACHE_WRITE_INPUT_MULTIPLIER = 1.25
CACHE_READ_INPUT_MULTIPLIER = 0.1


def _sse_documents(flow: dict[str, Any]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    pending = ""
    for chunk in flow["response"].get("sse_chunks", []):
        pending += sse_chunk_bytes(chunk).decode("utf-8", errors="replace")
        while separator := re.search(r"\r?\n\r?\n", pending):
            event = pending[: separator.start()]
            pending = pending[separator.end() :]
            data = "\n".join(
                line.removesuffix("\r").removeprefix("data:").lstrip()
                for line in event.splitlines()
                if line.startswith("data:")
            )
            if not data or data == "[DONE]":
                continue
            try:
                document = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(document, dict):
                documents.append(document)
    return documents


def _usage(flow: dict[str, Any]) -> tuple[int, int, int, int]:
    input_tokens = 0
    output_tokens = 0
    cache_write_tokens = 0
    cache_read_tokens = 0
    for document in _sse_documents(flow):
        usage = document.get("usage")
        if not isinstance(usage, dict):
            message = document.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            input_tokens = max(input_tokens, int(usage.get("input_tokens", 0) or 0))
            output_tokens = max(output_tokens, int(usage.get("output_tokens", 0) or 0))
            cache_write_tokens = max(
                cache_write_tokens,
                int(usage.get("cache_creation_input_tokens", 0) or 0),
            )
            cache_read_tokens = max(
                cache_read_tokens,
                int(usage.get("cache_read_input_tokens", 0) or 0),
            )
    return input_tokens, output_tokens, cache_write_tokens, cache_read_tokens


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cassette", type=Path, required=True)
    parser.add_argument(
        "--determinism-results",
        type=Path,
        default=Path("results/determinism.json"),
    )
    parser.add_argument("--out", type=Path, default=Path("results/benchmark.json"))
    parser.add_argument(
        "--table-out",
        type=Path,
        default=Path("results/benchmark.md"),
    )
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    arguments = parser.parse_args()

    reader = CassetteReader(arguments.cassette.resolve())
    manifest = reader.load_manifest()
    flows = reader.load_flows().flows
    model = ""
    input_tokens = 0
    output_tokens = 0
    cache_write_tokens = 0
    cache_read_tokens = 0
    model_calls = 0
    for flow in flows:
        key = flow.get("key", {})
        if key.get("host") != "api.anthropic.com" or key.get("path") != "/v1/messages":
            continue
        model_calls += 1
        request_body = reader.read_body(flow["request"]["body"])
        try:
            request_document = json.loads(request_body)
        except json.JSONDecodeError:
            request_document = {}
        if isinstance(request_document, dict) and not model:
            model = str(request_document.get("model", ""))
        flow_input, flow_output, flow_cache_write, flow_cache_read = _usage(flow)
        input_tokens += flow_input
        output_tokens += flow_output
        cache_write_tokens += flow_cache_write
        cache_read_tokens += flow_cache_read

    default_prices = MODEL_PRICES.get(model)
    input_price = arguments.input_price_per_million
    output_price = arguments.output_price_per_million
    if input_price is None or output_price is None:
        if default_prices is None:
            parser.error(
                f"no built-in pricing for model {model!r}; pass both price options"
            )
        input_price = default_prices[0] if input_price is None else input_price
        output_price = default_prices[1] if output_price is None else output_price

    replay_wall_time: float | None = None
    if arguments.determinism_results.is_file():
        determinism = json.loads(
            arguments.determinism_results.read_text(encoding="utf-8")
        )
        replay_wall_time = float(determinism["wall_time_seconds"]["median"])
    elif (arguments.cassette / "last-replay.json").is_file():
        last_replay = json.loads(
            (arguments.cassette / "last-replay.json").read_text(encoding="utf-8")
        )
        replay_wall_time = float(last_replay["wall_time_seconds"])

    cost = (
        input_tokens * input_price
        + output_tokens * output_price
        + cache_write_tokens * input_price * CACHE_WRITE_INPUT_MULTIPLIER
        + cache_read_tokens * input_price * CACHE_READ_INPUT_MULTIPLIER
    ) / 1_000_000
    record_wall_time = manifest.get("record_wall_time_seconds")
    result = {
        "model": model,
        "model_calls": model_calls,
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "cache_write": cache_write_tokens,
            "cache_read": cache_read_tokens,
        },
        "pricing_usd_per_million_tokens": {
            "input": input_price,
            "output": output_price,
            "cache_write_input_multiplier": CACHE_WRITE_INPUT_MULTIPLIER,
            "cache_read_input_multiplier": CACHE_READ_INPUT_MULTIPLIER,
            "source": PRICING_SOURCE,
            "verified": PRICING_VERIFIED,
        },
        "recorded": {
            "wall_time_seconds": record_wall_time,
            "estimated_cost_usd": cost,
        },
        "replayed": {
            "wall_time_seconds": replay_wall_time,
            "estimated_cost_usd": 0.0,
        },
    }
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    def _render_seconds(value: float | None) -> str:
        return f"{value:.3f}" if isinstance(value, (int, float)) else "n/a"

    arguments.table_out.parent.mkdir(parents=True, exist_ok=True)
    arguments.table_out.write_text(
        "| Mode | Wall time (s) | Estimated API cost (USD) |\n"
        "|---|---:|---:|\n"
        f"| Record | {_render_seconds(record_wall_time)} | {cost:.6f} |\n"
        f"| Replay | {_render_seconds(replay_wall_time)} | 0.000000 |\n",
        encoding="utf-8",
    )
    print(f"wrote {arguments.out} and {arguments.table_out}")


if __name__ == "__main__":
    main()
