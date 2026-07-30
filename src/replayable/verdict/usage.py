"""SSE usage extraction and deterministic token-cost accounting."""

from __future__ import annotations

import gzip
import json
import re
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from replayable.cassette import CassetteError, sse_chunk_bytes

PRICING_SOURCE = "https://docs.anthropic.com/en/docs/about-claude/pricing"
PRICING_VERIFIED = "2026-07-18"
MODEL_PRICES_USD_PER_MILLION = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
CACHE_WRITE_INPUT_MULTIPLIER = 1.25
CACHE_READ_INPUT_MULTIPLIER = 0.1


@dataclass(frozen=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_write": self.cache_write,
            "cache_read": self.cache_read,
        }

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.input, self.output, self.cache_write, self.cache_read


def _header_value(flow: dict[str, Any], name: str) -> str:
    response = flow.get("response")
    if not isinstance(response, dict):
        return ""
    headers = response.get("headers", [])
    if not isinstance(headers, list):
        return ""
    for header in headers:
        if (
            isinstance(header, (list, tuple))
            and len(header) == 2
            and isinstance(header[0], str)
            and isinstance(header[1], str)
            and header[0].lower() == name.lower()
        ):
            return header[1]
    return ""


def _decode_content(chunks: Iterable[bytes], content_encoding: str) -> bytes:
    payload = b"".join(chunks)
    encoding = content_encoding.strip().lower()
    try:
        if encoding in ("", "identity"):
            return payload
        if encoding in ("gzip", "x-gzip"):
            return gzip.decompress(payload)
        if encoding == "deflate":
            try:
                return zlib.decompress(payload)
            except zlib.error:
                return zlib.decompress(payload, -zlib.MAX_WBITS)
    except (EOFError, OSError, zlib.error):
        return b""
    return b""


def sse_documents(flow: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse complete SSE data documents across transport and CRLF boundaries."""

    response = flow.get("response")
    if not isinstance(response, dict):
        return []
    encoded_chunks = response.get("sse_chunks", [])
    if not isinstance(encoded_chunks, list):
        return []
    if not all(isinstance(chunk, dict) for chunk in encoded_chunks):
        return []
    try:
        chunks = [sse_chunk_bytes(chunk) for chunk in encoded_chunks]
    except CassetteError:
        return []
    decoded = _decode_content(chunks, _header_value(flow, "content-encoding"))
    text = decoded.decode("utf-8", errors="replace")

    documents: list[dict[str, Any]] = []
    for event in re.split(r"\r?\n\r?\n", text):
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


def _token_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(count, 0)


def extract_usage(
    flow: dict[str, Any],
    *,
    response_body: bytes | None = None,
) -> TokenUsage | None:
    """Return the maximum cumulative token counters reported by one model call."""

    documents = sse_documents(flow)
    if not documents:
        response = flow.get("response")
        if not isinstance(response, dict):
            return None
        if response_body is None:
            representation = response.get("body")
            if isinstance(representation, dict) and set(representation) == {"inline_utf8"}:
                inline = representation.get("inline_utf8")
                response_body = inline.encode() if isinstance(inline, str) else None
        if response_body is not None:
            decoded = _decode_content(
                [response_body],
                _header_value(flow, "content-encoding"),
            )
            try:
                document = json.loads(decoded)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            else:
                if isinstance(document, dict):
                    documents = [document]

    usage_documents: list[dict[str, Any]] = []
    for document in documents:
        usage = document.get("usage")
        if not isinstance(usage, dict):
            message = document.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            usage_documents.append(usage)
    if not usage_documents:
        return None
    return TokenUsage(
        input=max(_token_count(value.get("input_tokens")) for value in usage_documents),
        output=max(_token_count(value.get("output_tokens")) for value in usage_documents),
        cache_write=max(
            _token_count(value.get("cache_creation_input_tokens")) for value in usage_documents
        ),
        cache_read=max(
            _token_count(value.get("cache_read_input_tokens")) for value in usage_documents
        ),
    )


def estimate_cost_usd(model: str, usage: TokenUsage) -> float | None:
    prices = MODEL_PRICES_USD_PER_MILLION.get(model)
    if prices is None:
        return None
    input_price, output_price = prices
    return (
        usage.input * input_price
        + usage.output * output_price
        + usage.cache_write * input_price * CACHE_WRITE_INPUT_MULTIPLIER
        + usage.cache_read * input_price * CACHE_READ_INPUT_MULTIPLIER
    ) / 1_000_000
