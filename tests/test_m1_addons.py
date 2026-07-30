from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from collections.abc import Callable
from datetime import datetime

from mitmproxy import certs, http

from replayable.addons.fork_addon import ForkReplayAddon
from replayable.addons.record_addon import RecordAddon
from replayable.addons.replay_addon import (
    LEAF_CERT_VALIDITY,
    LEAF_CERT_VALIDITY_MARGIN,
    ReplayAddon,
    _pin_leaf_certificate_validity,
)
from replayable.cassette import CassetteReader, CassetteWriter, base_manifest
from replayable.cassette.events import EventLogReader


def initialize_cassette(path) -> None:
    CassetteWriter(path).initialize(
        base_manifest(
            created_at="2026-07-14T00:00:00Z",
            t0_epoch=0.0,
            image_ref="test-image",
            image_digest="sha256:test",
            command=["workload"],
            environment_fingerprint="sha256:env",
        )
    )


def make_flow(
    method: str,
    url: str,
    *,
    request_body: bytes = b"",
    response_body: bytes | None = None,
    status: int = 200,
    content_type: str = "application/octet-stream",
) -> http.HTTPFlow:
    flow = http.HTTPFlow(None, None)
    flow.request = http.Request.make(method, url, content=request_body)
    if response_body is not None:
        flow.response = http.Response.make(
            status,
            response_body,
            {"content-type": content_type, "x-recorded": "yes"},
        )
    return flow


def test_replay_leaf_certificate_window_includes_the_recorded_clock(monkeypatch):
    """An old cassette must not receive a leaf certificate dated two days ago."""

    now = datetime(2026, 7, 29, 12, 0, 0)
    recorded = datetime(2020, 1, 2, 3, 4, 5)
    monkeypatch.setattr(certs, "CERT_VALIDITY_OFFSET", certs.CERT_VALIDITY_OFFSET)
    monkeypatch.setattr(certs, "CERT_EXPIRY", certs.CERT_EXPIRY)

    _pin_leaf_certificate_validity(recorded.timestamp(), now=now)

    assert now + certs.CERT_VALIDITY_OFFSET == recorded - LEAF_CERT_VALIDITY_MARGIN
    assert certs.CERT_EXPIRY == LEAF_CERT_VALIDITY
    assert now + certs.CERT_VALIDITY_OFFSET + certs.CERT_EXPIRY == (
        recorded + LEAF_CERT_VALIDITY_MARGIN
    )


def test_record_addon_writes_m2_schema_and_redacts_before_hashing(tmp_path):
    initialize_cassette(tmp_path)
    flow = make_flow(
        "POST",
        "https://httpbin.org/post?mode=test",
        request_body=b'{"token":"real-token"}',
        response_body=b"\x00real-token\xff",
        status=201,
    )

    RecordAddon(tmp_path, {"API_TOKEN": "real-token"}).response(flow)

    reader = CassetteReader(tmp_path)
    record = reader.load_flows().flows[0]
    request_body = b'{"token":"[REDACTED:API_TOKEN]"}'
    response_body = b"\x00[REDACTED:API_TOKEN]\xff"
    assert record["seq"] == 1
    assert record["key"] == {
        "method": "POST",
        "host": "httpbin.org",
        "port": 443,
        "path": "/post",
    }
    assert record["request"]["query"] == "mode=test"
    assert record["request"]["body_sha256"] == hashlib.sha256(request_body).hexdigest()
    assert record["response"]["status"] == 201
    assert record["response"]["body_sha256"] == hashlib.sha256(response_body).hexdigest()
    assert reader.read_body(record["response"]["body"]) == response_body
    assert "real-token" not in (tmp_path / "flows.jsonl").read_text(encoding="utf-8")
    (event,) = EventLogReader(tmp_path).load_events()
    assert event.payload["flow"] == record
    assert event.payload["duration_seconds"] >= 0
    assert "real-token" not in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


def test_record_addon_enriches_anthropic_event_with_usage_and_cost(tmp_path):
    initialize_cassette(tmp_path)
    recorder = RecordAddon(tmp_path, {})
    flow = make_flow(
        "POST",
        "https://api.anthropic.com/v1/messages",
        request_body=b'{"model":"claude-haiku-4-5"}',
        response_body=b"",
        content_type="text/event-stream",
    )
    recorder.responseheaders(flow)
    stream = flow.response.stream
    assert isinstance(stream, Callable)
    chunks = [
        (
            b'event: message_start\r\ndata: {"type":"message_start",'
            b'"message":{"usage":{"input_tokens":100,"output_tokens":1}}}\r'
        ),
        (
            b'\n\r\nevent: message_delta\r\ndata: {"type":"message_delta",'
            b'"usage":{"output_tokens":25}}\r\n\r\n'
        ),
    ]
    for chunk in chunks:
        stream(chunk)

    recorder.response(flow)

    (event,) = EventLogReader(tmp_path).load_events()
    assert event.payload["metrics"] == {
        "model": "claude-haiku-4-5",
        "usage_available": True,
        "tokens": {
            "input": 100,
            "output": 25,
            "cache_write": 0,
            "cache_read": 0,
        },
        "estimated_cost_usd": 0.000225,
    }


def test_replay_addon_serves_duplicate_requests_fifo_byte_identically(tmp_path):
    initialize_cassette(tmp_path)
    recorder = RecordAddon(tmp_path, {})
    recorder.response(
        make_flow("GET", "https://api.github.com/zen", response_body=b"first\n")
    )
    recorder.response(
        make_flow("GET", "https://api.github.com/zen", response_body=b"second\n")
    )

    replay = ReplayAddon(
        tmp_path,
        tmp_path / "replay-report.json",
        tmp_path / "replay-state.json",
    )
    replay.load(None)
    first = make_flow("GET", "https://api.github.com/zen")
    second = make_flow("GET", "https://api.github.com/zen")

    replay.request(first)
    replay.request(second)

    assert first.response.status_code == 200
    assert first.response.raw_content == b"first\n"
    assert second.response.raw_content == b"second\n"
    assert second.response.headers["x-recorded"] == "yes"


def test_fork_addon_serves_exact_prefix_then_captures_live_without_mutating_baseline(
    tmp_path,
):
    baseline = tmp_path / "baseline"
    capture = tmp_path / "capture"
    initialize_cassette(baseline)
    initialize_cassette(capture)
    recorder = RecordAddon(baseline, {})
    recorder.response(make_flow("GET", "https://api.github.com/zen", response_body=b"pinned\n"))
    recorder.response(make_flow("GET", "https://example.test/live", response_body=b"recorded\n"))

    fork = ForkReplayAddon(
        baseline,
        capture,
        tmp_path / "fork-report.json",
        tmp_path / "fork-state.json",
        fork_at=1,
        secrets={},
    )
    fork.load(None)
    pinned = make_flow("GET", "https://api.github.com/zen")
    live = make_flow("GET", "https://example.test/live")

    fork.request(pinned)
    fork.request(live)
    assert pinned.response.raw_content == b"pinned\n"
    assert live.response is None

    live.response = http.Response.make(200, b"fresh\n")
    fork.response(live)
    fork.done()

    assert len(CassetteReader(baseline).load_flows().flows) == 2
    captured = CassetteReader(capture).load_flows().flows
    assert len(captured) == 1
    assert captured[0]["key"]["host"] == "example.test"
    assert CassetteReader(capture).read_body(captured[0]["response"]["body"]) == b"fresh\n"
    state = json.loads((tmp_path / "fork-state.json").read_text(encoding="utf-8"))
    assert state["pinned_served"] == 1
    assert state["live_requests"] == 1
    assert state["live_responses"] == 1
    assert state["live_errors"] == 0


def test_fork_addon_never_switches_live_before_pinned_prefix_matches(tmp_path):
    baseline = tmp_path / "baseline"
    capture = tmp_path / "capture"
    initialize_cassette(baseline)
    initialize_cassette(capture)
    RecordAddon(baseline, {}).response(
        make_flow("GET", "https://api.github.com/zen", response_body=b"pinned\n")
    )
    fork = ForkReplayAddon(
        baseline,
        capture,
        tmp_path / "fork-report.json",
        tmp_path / "fork-state.json",
        fork_at=1,
        secrets={},
    )
    fork.load(None)
    divergent = make_flow("GET", "https://example.test/live")

    fork.request(divergent)

    assert divergent.response.status_code == 599
    assert CassetteReader(capture).load_flows().flows == []
    state = json.loads((tmp_path / "fork-state.json").read_text(encoding="utf-8"))
    assert state["pinned_served"] == 0
    assert state["live_requests"] == 0


def test_fork_mismatch_report_redacts_live_secret_values(tmp_path):
    baseline = tmp_path / "baseline"
    capture = tmp_path / "capture"
    initialize_cassette(baseline)
    initialize_cassette(capture)
    RecordAddon(baseline, {}).response(
        make_flow(
            "POST",
            "https://api.example.test/v1",
            request_body=b'{"prompt":"recorded"}',
            response_body=b"ok",
            content_type="application/json",
        )
    )
    fork = ForkReplayAddon(
        baseline,
        capture,
        tmp_path / "fork-report.json",
        tmp_path / "fork-state.json",
        fork_at=1,
        secrets={"API_TOKEN": "harmful-secret"},
    )
    fork.load(None)
    divergent = make_flow(
        "POST",
        "https://api.example.test/v1",
        request_body=b'{"prompt":"harmful-secret"}',
        content_type="application/json",
    )
    divergent.request.headers["content-type"] = "application/json"

    fork.request(divergent)

    report = (tmp_path / "fork-report.json").read_text(encoding="utf-8")
    assert "harmful-secret" not in report
    assert "[REDACTED:API_TOKEN]" in report


def test_sse_stream_callback_preserves_chunks_and_hashes_concatenation(tmp_path):
    initialize_cassette(tmp_path)
    recorder = RecordAddon(tmp_path, {})
    flow = make_flow(
        "GET",
        "https://events.test/stream",
        response_body=b"",
        content_type="text/event-stream",
    )

    recorder.responseheaders(flow)
    stream = flow.response.stream
    assert isinstance(stream, Callable)
    chunks = [b"data: one\n\n", b"data: two\n\n", b"data: done\n\n"]
    for chunk in chunks:
        assert stream(chunk) == chunk
    recorder.response(flow)

    record = CassetteReader(tmp_path).load_flows().flows[0]
    assert record["response"]["body"] is None
    assert record["response"]["sse_chunks"] == [
        {"data_utf8": chunk.decode()} for chunk in chunks
    ]
    assert record["response"]["body_sha256"] == hashlib.sha256(
        b"".join(chunks)
    ).hexdigest()

    replay = ReplayAddon(
        tmp_path,
        tmp_path / "replay-report.json",
        tmp_path / "replay-state.json",
    )
    replay.load(None)
    live = make_flow("GET", "https://events.test/stream")
    replay.request(live)
    replay_stream = live.response.stream
    assert isinstance(replay_stream, Callable)
    assert list(replay_stream(live.response.raw_content)) == chunks
    assert replay_stream(b"") == b""


def test_sse_chunk_boundary_splitting_a_utf8_codepoint_is_recorded_safely(tmp_path):
    initialize_cassette(tmp_path)
    recorder = RecordAddon(tmp_path, {})
    flow = make_flow(
        "GET",
        "https://events.test/stream",
        response_body=b"",
        content_type="text/event-stream",
    )

    recorder.responseheaders(flow)
    stream = flow.response.stream
    payload = 'data: {"text":"héllo ✓"}\n\n'.encode()
    # Split inside the two-byte "é" sequence, as TCP is free to do.
    split = payload.index("é".encode()) + 1
    chunks = [payload[:split], payload[split:]]
    for chunk in chunks:
        stream(chunk)
    recorder.response(flow)

    record = CassetteReader(tmp_path).load_flows().flows[0]
    stored_chunks = record["response"]["sse_chunks"]
    assert all("data_utf8" in chunk for chunk in stored_chunks)
    joined = "".join(chunk["data_utf8"] for chunk in stored_chunks).encode("utf-8")
    assert joined == payload
    assert record["response"]["body_sha256"] == hashlib.sha256(payload).hexdigest()

    replay = ReplayAddon(
        tmp_path,
        tmp_path / "replay-report.json",
        tmp_path / "replay-state.json",
    )
    replay.load(None)
    live = make_flow("GET", "https://events.test/stream")
    replay.request(live)
    assert live.response.raw_content == payload


def test_invalid_utf8_sse_chunks_fall_back_to_base64(tmp_path):
    initialize_cassette(tmp_path)
    recorder = RecordAddon(tmp_path, {})
    flow = make_flow(
        "GET",
        "https://events.test/stream",
        response_body=b"",
        content_type="text/event-stream",
    )

    recorder.responseheaders(flow)
    stream = flow.response.stream
    payload = b"data: \xff\xfe not utf-8\n\n"
    stream(payload)
    recorder.response(flow)

    record = CassetteReader(tmp_path).load_flows().flows[0]
    (stored_chunk,) = record["response"]["sse_chunks"]
    assert "data_base64" in stored_chunk

    replay = ReplayAddon(
        tmp_path,
        tmp_path / "replay-report.json",
        tmp_path / "replay-state.json",
    )
    replay.load(None)
    live = make_flow("GET", "https://events.test/stream")
    replay.request(live)
    assert live.response.raw_content == payload


def test_content_encoded_body_is_replayed_without_re_encoding(tmp_path):
    initialize_cassette(tmp_path)
    plaintext = b'{"hits":[{"title":"deterministic replay"}]}'
    recorded = make_flow("GET", "https://hn.test/search")
    recorded.response = http.Response.make(
        200,
        plaintext,
        {"content-type": "application/json", "content-encoding": "gzip"},
    )
    compressed = recorded.response.raw_content
    assert compressed.startswith(b"\x1f\x8b")
    RecordAddon(tmp_path, {}).response(recorded)

    replay = ReplayAddon(
        tmp_path,
        tmp_path / "replay-report.json",
        tmp_path / "replay-state.json",
    )
    replay.load(None)
    live = make_flow("GET", "https://hn.test/search")
    replay.request(live)

    # The wire bytes must be identical, not gzip(gzip(...)): a client that
    # decodes content-encoding once has to end up back at the plaintext.
    assert live.response.raw_content == compressed
    assert live.response.content == plaintext
    assert live.response.headers["content-length"] == str(len(compressed))


def test_content_encoded_sse_chunks_are_replayed_without_re_encoding(tmp_path):
    initialize_cassette(tmp_path)
    frames = [b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"]
    compressor = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
    chunks = [
        compressor.compress(frame) + compressor.flush(zlib.Z_SYNC_FLUSH)
        for frame in frames
    ]
    chunks[-1] += compressor.flush()

    recorder = RecordAddon(tmp_path, {})
    recorded = make_flow("POST", "https://api.test/v1/messages")
    recorded.response = http.Response.make(
        200,
        b"",
        {"content-type": "text/event-stream", "content-encoding": "gzip"},
    )
    recorder.responseheaders(recorded)
    for chunk in chunks:
        recorded.response.stream(chunk)
    recorder.response(recorded)

    replay = ReplayAddon(
        tmp_path,
        tmp_path / "replay-report.json",
        tmp_path / "replay-state.json",
    )
    replay.load(None)
    live = make_flow("POST", "https://api.test/v1/messages")
    replay.request(live)

    assert live.response.raw_content == b"".join(chunks)
    assert gzip.decompress(live.response.raw_content) == b"".join(frames)


def test_unmatched_request_returns_599_and_writes_marker(tmp_path):
    initialize_cassette(tmp_path)
    mismatch_path = tmp_path / "replay-report.json"
    replay = ReplayAddon(
        tmp_path,
        mismatch_path,
        tmp_path / "replay-state.json",
    )
    replay.load(None)
    flow = make_flow("DELETE", "https://example.test/not-recorded")

    replay.request(flow)

    assert flow.response.status_code == 599
    response_payload = json.loads(flow.response.raw_content)
    assert response_payload["live_request"]["method"] == "DELETE"
    assert response_payload["live_request"]["path"] == "/not-recorded"
    mismatch = json.loads(mismatch_path.read_text(encoding="utf-8"))
    assert mismatch == response_payload
    assert mismatch["live_request"]["method"] == "DELETE"
    assert mismatch["live_request"]["path"] == "/not-recorded"
    assert mismatch["nearest_candidates"] == []
