from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from replayable.exit_codes import ExitCode
from replayable.runner import record_run, replay_run

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("REPLAYABLE_RUN_E2E") != "1",
        reason="set REPLAYABLE_RUN_E2E=1 after building the base image",
    ),
]


class OrderedResponseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    request_number = 0
    lock = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        with self.lock:
            type(self).request_number += 1
            response_body = f"response-{type(self).request_number}".encode()
        self.send_response(200)
        self.send_header("content-type", "text/plain")
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


def workload(target: str, prompt: str) -> str:
    return (
        "i=1; while [ $i -le 5 ]; do "
        "uuid=$(python -c 'import uuid; print(uuid.uuid4())'); "
        "ts=$(date +%s); "
        'curl --proxy "$HTTPS_PROXY" --fail-with-body --silent --show-error '
        "-H 'content-type: application/json' "
        f'--data "{{\\"tool_call_id\\":\\"$uuid\\",'
        f'\\"sent_time\\":\\"$ts\\",\\"prompt\\":\\"{prompt}\\"}}" '
        f"{target}/messages; "
        "printf '\\n'; i=$((i + 1)); done"
    )


def test_uuid_timestamp_torture_fifo_and_prompt_change_report(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    OrderedResponseHandler.request_number = 0
    server = ThreadingHTTPServer(("0.0.0.0", 0), OrderedResponseHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    target = f"http://{docker_host_address()}:{server.server_port}"
    cassette = tmp_path / "cassette"

    try:
        record_result = record_run(
            image="replayable/agent-base:local",
            command=["sh", "-c", workload(target, "same prompt")],
            out=cassette,
            port=available_port(),
        )
        recorded_output = capfd.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert record_result == ExitCode.SUCCESS
    assert recorded_output.splitlines() == [
        "response-1",
        "response-2",
        "response-3",
        "response-4",
        "response-5",
    ]

    replay_result = replay_run(
        cassette=cassette,
        strict=True,
        port=available_port(),
    )
    replayed_output = capfd.readouterr().out
    assert replay_result == ExitCode.SUCCESS
    assert replayed_output == recorded_output + (
        "DETERMINISTIC ✓ (workspace sha256 matches)\n"
    )
    assert (cassette / "agent.stdout").read_bytes() == (
        cassette / "replay-agent.stdout"
    ).read_bytes()

    manifest_path = cassette / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["command"] = ["sh", "-c", workload(target, "changed prompt")]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    mismatch_result = replay_run(cassette=cassette, port=available_port())
    mismatch_output = capfd.readouterr()
    assert mismatch_result == ExitCode.REPLAY_MISMATCH
    assert "mismatch: POST /messages" in mismatch_output.err
    assert "changed prompt" in mismatch_output.err
    report = json.loads((cassette / "replay-report.json").read_text(encoding="utf-8"))
    assert report["live_request"]["path"] == "/messages"
    assert "same prompt" in report["diff"]
    assert "changed prompt" in report["diff"]
