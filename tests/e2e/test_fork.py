from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from replayable.exit_codes import ExitCode
from replayable.runner import default_ca_path, record_run, replay_run

pytestmark = pytest.mark.e2e


def test_fork_serves_prefix_then_reaches_local_upstream(tmp_path):
    if os.environ.get("REPLAYABLE_RUN_E2E") != "1":
        pytest.skip("set REPLAYABLE_RUN_E2E=1 to run Docker acceptance tests")

    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            body = f"live-{len(requests)}\n".encode()
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            host_address = probe.getsockname()[0]
        finally:
            probe.close()
        url = f"http://{host_address}:{port}/value"
        source = (
            "import urllib.request; "
            f"url={url!r}; "
            "[print(urllib.request.urlopen(url).read().decode().strip()) "
            "for _ in range(4)]"
        )
        cassette = tmp_path / "cassette"
        assert (
            record_run(
                image="replayable/agent-base:local",
                command=["python", "-c", source],
                out=cassette,
                ca_path=default_ca_path(),
            )
            == ExitCode.SUCCESS
        )
        assert len(requests) == 4

        result_code = replay_run(
            cassette=cassette,
            fork_at=2,
            ca_path=default_ca_path(),
        )

        assert result_code == ExitCode.REPLAY_MISMATCH
        assert len(requests) == 6
        result = json.loads((cassette / "fork-result.json").read_text(encoding="utf-8"))
        assert result["segments"]["pinned"]["served_flow_count"] == 2
        assert result["segments"]["live"]["flow_count"] == 2
        assert result["segments"]["live"]["estimated_cost_usd"] == 0.0
        assert result["downstream"]["stdout"]["matches"] is False
        assert result["events"][0]["key"].endswith("/value")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
