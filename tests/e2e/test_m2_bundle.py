from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from replayable.cassette import CassetteReader
from replayable.exit_codes import ExitCode
from replayable.runner import record_run, replay_run

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("REPLAYABLE_RUN_E2E") != "1",
        reason="set REPLAYABLE_RUN_E2E=1 after building the base image",
    ),
]

SSE_CHUNKS = [
    b"event: message\ndata: one\n\n",
    b"event: message\ndata: two\n\n",
    b"event: done\ndata: complete\n\n",
]


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path != "/sse":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        for chunk in SSE_CHUNKS:
            self.wfile.write(chunk)
            self.wfile.flush()
        self.close_connection = True

    def do_POST(self) -> None:
        if self.path != "/echo":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        request_body = self.rfile.read(length).decode()
        response_body = json.dumps(
            {
                "authorization": self.headers.get("authorization"),
                "body": request_body,
            },
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("set-cookie", f"session={request_body}")
        self.send_header("content-length", str(len(response_body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(response_body)
        self.close_connection = True

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def docker_host_address() -> str:
    if platform.system() != "Linux":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("192.0.2.1", 80))
            return route.getsockname()[0]
    completed = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_sse_recording_offline_replay_and_bundle_secret_redaction(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 0), FixtureHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    target = f"http://{docker_host_address()}:{server.server_port}"
    sse_cassette = tmp_path / "sse-cassette"
    secret_cassette = tmp_path / "secret-cassette"
    secret = "m2-real-secret-value"
    env_file = tmp_path / ".env"
    env_file.write_text(f"API_TOKEN={secret}\n", encoding="utf-8")

    try:
        record_result = record_run(
            image="replayable/agent-base:local",
            command=[
                "sh",
                "-c",
                (
                    'curl --proxy "$HTTPS_PROXY" --fail --silent --no-buffer '
                    f"{target}/sse"
                ),
            ],
            out=sse_cassette,
            port=available_port(),
        )
        recorded_sse_output = capfd.readouterr().out.encode()

        secret_result = record_run(
            image="replayable/agent-base:local",
            command=[
                "sh",
                "-c",
                (
                    'curl --proxy "$HTTPS_PROXY" --fail --silent '
                    '-H "Authorization: Bearer $API_TOKEN" '
                    '--data "$API_TOKEN" '
                    f"{target}/echo"
                ),
            ],
            env_file=env_file,
            out=secret_cassette,
            port=available_port(),
        )
        capfd.readouterr()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert record_result == ExitCode.SUCCESS
    assert secret_result == ExitCode.SUCCESS

    sse_reader = CassetteReader(sse_cassette)
    sse_manifest = sse_reader.load_manifest()
    sse_flow = sse_reader.load_flows().flows[0]
    stored_chunks = sse_flow["response"]["sse_chunks"]
    stored_body = "".join(chunk["data_utf8"] for chunk in stored_chunks).encode()
    assert sse_manifest["flow_count"] == 1
    assert sse_flow["response"]["body"] is None
    assert stored_body == b"".join(SSE_CHUNKS)
    assert sse_flow["response"]["body_sha256"] == hashlib.sha256(stored_body).hexdigest()
    assert recorded_sse_output == stored_body

    # The origin server is now stopped. A successful replay proves that the
    # request-hook response path does not need an upstream connection.
    replay_result = replay_run(cassette=sse_cassette, port=available_port())
    replayed_sse_output = capfd.readouterr().out.encode()
    assert replay_result == ExitCode.SUCCESS
    assert replayed_sse_output == recorded_sse_output + (
        b"DETERMINISTIC \xe2\x9c\x93 (workspace sha256 matches)\n"
    )
    assert (sse_cassette / "agent.stdout").read_bytes() == (
        sse_cassette / "replay-agent.stdout"
    ).read_bytes()

    secret_reader = CassetteReader(secret_cassette)
    secret_flow = secret_reader.load_flows().flows[0]
    assert secret_reader.load_manifest()["flow_count"] == 1
    assert ["authorization", "[REDACTED]"] in secret_flow["request"]["headers"]
    assert ["set-cookie", "[REDACTED]"] in secret_flow["response"]["headers"]
    assert b"[REDACTED:API_TOKEN]" in secret_reader.read_body(
        secret_flow["request"]["body"]
    )
    assert b"[REDACTED:API_TOKEN]" in secret_reader.read_body(
        secret_flow["response"]["body"]
    )
    for path in secret_cassette.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), f"secret leaked into {path}"
