"""Milestone 2 mitmproxy cassette recording addon."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mitmproxy import http

from replayable.cassette import (
    DEFAULT_REDACTED_HEADERS,
    CassetteReader,
    CassetteWriter,
    sha256_bytes,
)
from replayable.cassette.events import event_from_flow
from replayable.redact import redact_body, redact_headers
from replayable.verdict.usage import estimate_cost_usd, extract_usage


def _environment_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return Path(value)


def _environment_secrets() -> dict[str, str]:
    """Load secret values from the runner's private 0600 file, never from env."""

    secrets_file = os.environ.get("REPLAYABLE_SECRET_VALUES_FILE")
    if not secrets_file:
        return {}
    try:
        value = json.loads(Path(secrets_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"secret values file is unreadable: {exc}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(secret, str)
        for name, secret in value.items()
    ):
        raise RuntimeError("secret values file must contain a string mapping")
    return value


class RecordAddon:
    """Append redacted flow records as responses complete."""

    def __init__(
        self,
        cassette_directory: Path | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.cassette_directory = cassette_directory
        self.secrets = secrets
        self.writer: CassetteWriter | None = None
        self.t0_epoch: float | None = None
        self.sequence = 0
        self.sse_chunks: dict[str, list[bytes]] = {}

    def load(self, _loader: Any) -> None:
        directory = self.cassette_directory or _environment_path(
            "REPLAYABLE_CASSETTE_DIR"
        )
        self.cassette_directory = directory
        self.writer = CassetteWriter(directory)
        if self.secrets is None:
            self.secrets = _environment_secrets()

    def _require_writer(self) -> CassetteWriter:
        if self.writer is None:
            if self.cassette_directory is None:
                raise RuntimeError("record addon is not loaded")
            self.writer = CassetteWriter(self.cassette_directory)
        return self.writer

    def _t0(self) -> float:
        if self.t0_epoch is None:
            if self.cassette_directory is None:
                raise RuntimeError("record addon is not loaded")
            manifest = CassetteReader(self.cassette_directory).load_manifest()
            self.t0_epoch = float(manifest["t0_epoch"])
        return self.t0_epoch

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        content_type = flow.response.headers.get("content-type", "")
        if content_type.lower().startswith("text/event-stream"):
            self.sse_chunks[flow.id] = []

            def capture(chunk: bytes) -> bytes:
                self.sse_chunks[flow.id].append(chunk)
                return chunk

            flow.response.stream = capture

    def response(self, flow: http.HTTPFlow) -> None:
        writer = self._require_writer()
        secrets = self.secrets or {}
        request_body = redact_body(flow.request.raw_content or b"", secrets)
        is_sse = flow.id in self.sse_chunks
        raw_chunks = self.sse_chunks.pop(flow.id, [])
        if is_sse:
            response_chunks = _redact_sse_chunks(raw_chunks, secrets)
            response_body = b"".join(response_chunks)
        else:
            response_chunks = []
            response_body = redact_body(flow.response.raw_content or b"", secrets)

        self.sequence += 1
        parsed_url = urlsplit(flow.request.pretty_url)
        response_record: dict[str, Any] = {
            "status": flow.response.status_code,
            "headers": redact_headers(
                flow.response.headers.items(multi=True),
                DEFAULT_REDACTED_HEADERS,
            ),
            "body": None if is_sse else writer.represent_body(response_body),
            "body_sha256": sha256_bytes(response_body),
        }
        if is_sse:
            response_record["sse_chunks"] = _encode_sse_chunks(response_chunks)

        started_at = flow.request.timestamp_start or time.time()
        completed_at = flow.response.timestamp_end or time.time()
        record = {
            "seq": self.sequence,
            "key": {
                "method": flow.request.method.upper(),
                "host": parsed_url.hostname or flow.request.host.lower(),
                "port": parsed_url.port or flow.request.port,
                "path": parsed_url.path or "/",
            },
            "request": {
                "query": parsed_url.query,
                "headers": redact_headers(
                    flow.request.headers.items(multi=True),
                    DEFAULT_REDACTED_HEADERS,
                ),
                "body": writer.represent_body(request_body),
                "body_sha256": sha256_bytes(request_body),
            },
            "response": response_record,
            "timing": {
                "started": max(0.0, started_at - self._t0()),
                "completed": max(0.0, completed_at - self._t0()),
            },
        }
        metrics: dict[str, Any] | None = None
        if (
            record["key"]["host"] == "api.anthropic.com"
            and record["key"]["path"] == "/v1/messages"
        ):
            try:
                request_document = json.loads(request_body)
            except json.JSONDecodeError:
                request_document = {}
            model = (
                str(request_document.get("model", ""))
                if isinstance(request_document, dict)
                else ""
            )
            usage = extract_usage(record, response_body=response_body)
            metrics = {
                "model": model,
                "usage_available": usage is not None,
            }
            if usage is not None:
                metrics["tokens"] = usage.as_dict()
                estimated_cost = estimate_cost_usd(model, usage)
                if estimated_cost is not None:
                    metrics["estimated_cost_usd"] = estimated_cost
        event = event_from_flow(record, lamport=self.sequence, metrics=metrics)
        writer.append_flow(record, event=event)


def _redact_sse_chunks(
    chunks: list[bytes],
    secrets: dict[str, str],
) -> list[bytes]:
    """Redact SSE while retaining original callback boundaries when possible."""

    individually_redacted = [redact_body(chunk, secrets) for chunk in chunks]
    globally_redacted = redact_body(b"".join(chunks), secrets)
    if b"".join(individually_redacted) == globally_redacted:
        return individually_redacted

    # A secret crossed a transport boundary. Security takes precedence over an
    # impossible exact boundary mapping after the replacement changes length.
    redacted_chunks: list[bytes] = []
    offset = 0
    for chunk in chunks[:-1]:
        next_offset = min(offset + len(chunk), len(globally_redacted))
        redacted_chunks.append(globally_redacted[offset:next_offset])
        offset = next_offset
    redacted_chunks.append(globally_redacted[offset:])
    return redacted_chunks


def _split_utf8_boundary(data: bytes) -> tuple[bytes, bytes]:
    """Split off an incomplete trailing UTF-8 sequence to carry into the next chunk."""

    for held in range(1, min(3, len(data)) + 1):
        byte = data[-held]
        if byte < 0x80:
            break
        if byte >= 0xC0:
            expected = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            if expected > held:
                return data[:-held], data[-held:]
            break
    return data, b""


def _encode_sse_chunk(data: bytes) -> dict[str, str]:
    try:
        return {"data_utf8": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"data_base64": base64.b64encode(data).decode("ascii")}


def _encode_sse_chunks(chunks: list[bytes]) -> list[dict[str, str]]:
    """JSON-encode transport chunks whose boundaries may split UTF-8 codepoints.

    Trailing incomplete sequences move to the next chunk, so the concatenated
    bytes (and the recorded body hash) are preserved exactly. Genuinely invalid
    UTF-8 falls back to a base64 representation instead of crashing the hook.
    """

    encoded: list[dict[str, str]] = []
    carry = b""
    for index, chunk in enumerate(chunks):
        data = carry + chunk
        carry = b""
        if index < len(chunks) - 1:
            data, carry = _split_utf8_boundary(data)
        encoded.append(_encode_sse_chunk(data))
    return encoded


addons = [RecordAddon()]
