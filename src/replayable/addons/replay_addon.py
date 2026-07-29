"""Milestone 3 normalized, offline mitmproxy replay addon."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from mitmproxy import http

from replayable.cassette import CassetteReader, sse_chunk_bytes
from replayable.matcher import (
    RawRequest,
    ReplayMismatch,
    RequestMatcher,
    normalize_request,
)
from replayable.normalize_rules import load_rules


class RecordedSSEStream:
    """Split mitmproxy's synthetic body into recorded SSE frame boundaries."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.emitted = False

    def __call__(self, incoming: bytes) -> bytes | Iterable[bytes]:
        if not incoming or self.emitted:
            return b""
        self.emitted = True
        return self.chunks


class ReplayAddon:
    """Serve normalized FIFO responses in the request hook, never upstream."""

    def __init__(
        self,
        cassette_directory: Path | None = None,
        report_path: Path | None = None,
        state_path: Path | None = None,
        rules_path: Path | None = None,
    ) -> None:
        self.cassette_directory = cassette_directory
        self.report_path = report_path
        self.state_path = state_path
        self.rules_path = rules_path
        self.reader: CassetteReader | None = None
        self.matcher: RequestMatcher | None = None

    def load(self, _loader: Any) -> None:
        cassette_directory = self.cassette_directory or _path_from_environment(
            "REPLAYABLE_CASSETTE_DIR"
        )
        self.cassette_directory = cassette_directory
        self.report_path = self.report_path or _path_from_environment(
            "REPLAYABLE_REPORT_FILE"
        )
        self.state_path = self.state_path or _path_from_environment(
            "REPLAYABLE_STATE_FILE"
        )
        if self.rules_path is None:
            configured_rules = os.environ.get("REPLAYABLE_RULES_FILE")
            self.rules_path = Path(configured_rules) if configured_rules else None

        self.reader = CassetteReader(cassette_directory)
        manifest = self.reader.load_manifest()
        flows = self.reader.load_flows().flows
        rules = load_rules(self.rules_path)
        recorded_ruleset = manifest.get("ruleset_version")
        if recorded_ruleset is not None and recorded_ruleset != rules.version:
            raise RuntimeError("normalization rules do not match cassette manifest")
        self.matcher = RequestMatcher.from_flows(flows, self.reader, rules)
        self._write_state()

    def request(self, flow: http.HTTPFlow) -> None:
        matcher = self._require_matcher()
        live_request = _raw_request_from_flow(flow)
        try:
            record = matcher.match(live_request)
        except ReplayMismatch as mismatch:
            self._write_report(mismatch.payload)
            self._write_state()
            flow.response = http.Response.make(
                599,
                json.dumps(mismatch.payload, indent=2, ensure_ascii=False) + "\n",
                {"content-type": "application/json; charset=utf-8"},
            )
            return

        self._write_state()
        response = record["response"]
        chunks = [
            sse_chunk_bytes(chunk)
            for chunk in response.get("sse_chunks", [])
        ]
        if response.get("sse_chunks") is not None:
            body = b"".join(chunks)
        else:
            if self.reader is None:
                raise RuntimeError("replay addon is not loaded")
            body = self.reader.read_body(response["body"])

        expected_sha = response["body_sha256"]
        if hashlib.sha256(body).hexdigest() != expected_sha:
            normalized = normalize_request(live_request, matcher.rules)
            payload = {
                "live_request": normalized.as_report_dict(),
                "nearest_candidates": [{"seq": record["seq"]}],
                "diff": "recorded response body hash mismatch",
            }
            self._write_report(payload)
            flow.response = http.Response.make(
                599,
                json.dumps(payload, indent=2) + "\n",
                {"content-type": "application/json; charset=utf-8"},
            )
            return

        replayed_response = http.Response.make(response["status"])
        replayed_response.headers = http.Headers(
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in response.get("headers", [])
        )
        # Recorded bodies are raw wire bytes, so they already carry the recorded
        # content-encoding. Assigning `.content` would re-apply that encoding and
        # hand the workload a double-compressed body, so write the raw bytes and
        # leave the recorded content-length untouched.
        replayed_response.raw_content = body
        if response.get("sse_chunks") is not None:
            replayed_response.headers.pop("content-length", None)
            replayed_response.stream = RecordedSSEStream(chunks)
        flow.response = replayed_response

    def done(self) -> None:
        if self.matcher is not None and self.state_path is not None:
            self._write_state()

    def _require_matcher(self) -> RequestMatcher:
        if self.matcher is None:
            raise RuntimeError("replay addon is not loaded")
        return self.matcher

    def _write_report(self, payload: dict[str, Any]) -> None:
        if self.report_path is None:
            raise RuntimeError("replay report path is not configured")
        if not self.report_path.exists():
            _write_json_atomic(self.report_path, payload)

    def _write_state(self) -> None:
        if self.state_path is None:
            raise RuntimeError("replay state path is not configured")
        matcher = self._require_matcher()
        _write_json_atomic(
            self.state_path,
            {"unconsumed_sequences": matcher.unconsumed_sequences()},
        )


def _raw_request_from_flow(flow: http.HTTPFlow) -> RawRequest:
    parsed = urlsplit(flow.request.pretty_url)
    return RawRequest(
        method=flow.request.method,
        host=parsed.hostname or flow.request.host,
        port=parsed.port or flow.request.port,
        path=parsed.path or "/",
        query=parsed.query,
        headers=list(flow.request.headers.items(multi=True)),
        body=flow.request.raw_content or b"",
        scheme=parsed.scheme or flow.request.scheme,
    )


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _path_from_environment(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return Path(value)


addons = [ReplayAddon()]
