from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from conftest import stub_manifest

from replayable.cassette import CassetteWriter
from replayable.exit_codes import ExitCode
from replayable.snapshot import create_snapshot
from replayable.ui_server import MAX_REQUEST_BODY, UIApp, create_server

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def make_cassette(root: Path, name: str = "demo") -> Path:
    cassette = root / name
    writer = CassetteWriter(cassette)
    writer.initialize(stub_manifest(created_at="2026-07-29T12:00:00Z"))
    writer.append_flow(
        {
            "seq": 1,
            "key": {
                "method": "POST",
                "host": "api.example.test",
                "port": 443,
                "path": "/v1/messages",
            },
            "request": {
                "query": "b=2&a=1",
                "headers": [["content-type", "application/json"]],
                "body": writer.represent_body(b'{"prompt":"hello","request_id":"volatile"}'),
                "body_sha256": "request-sha",
            },
            "response": {
                "status": 200,
                "headers": [["content-type", "application/json"]],
                "body": writer.represent_body(b'{"answer":"world"}'),
                "body_sha256": "response-sha",
            },
            "timing": {"started": 0.25, "completed": 0.75},
        }
    )
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    snapshot = create_snapshot(workspace, cassette)
    (cassette / "agent.stdout").write_bytes(b"")
    (cassette / "agent.stderr").write_bytes(b"")
    writer.update_manifest(
        flow_count=1,
        event_count=1,
        workspace_sha256=snapshot.sha256,
        stdout_sha256=EMPTY_SHA256,
        stderr_sha256=EMPTY_SHA256,
        record_exit_code=0,
        record_wall_time_seconds=1.0,
    )
    return cassette


def payload(response) -> dict:
    return json.loads(response.body)


def test_read_routes_return_stable_cassette_views(tmp_path):
    root = tmp_path / "cassettes"
    cassette = make_cassette(root)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
    (cassette / "replay-report.json").write_text(
        json.dumps(
            {
                "live_request": {"method": "POST", "path": "/v1/messages"},
                "nearest_candidates": [{"seq": 1}],
                "diff": "- recorded\n+ live\n",
            }
        ),
        encoding="utf-8",
    )
    app = UIApp(root, static_dir=static)

    listed = payload(app.handle("GET", "/api/cassettes"))
    assert listed == {
        "cassettes": [
            {
                "created_at": "2026-07-29T12:00:00Z",
                "flow_count": 1,
                "has_fork_result": False,
                "has_observation": False,
                "image": {
                    "digest": "sha256:image",
                    "ref": "image",
                },
                "last_exit_code": None,
                "name": "demo",
                "status": "mismatch",
            }
        ]
    }
    assert payload(app.handle("GET", "/api/cassettes/demo/timeline")) == {
        "events": [
            {
                "channel": "network",
                "duration_seconds": 0.5,
                "key": "POST api.example.test:443/v1/messages",
                "kind": "http.exchange",
                "lamport": 1,
                "scope": "api.example.test",
                "seq": 1,
                "stream_chunk_count": 0,
                "t_rel": 0.25,
            }
        ]
    }
    flow = payload(app.handle("GET", "/api/cassettes/demo/flows/1"))
    assert flow["request"]["body_decoded"] == ('{"prompt":"hello","request_id":"volatile"}')
    assert flow["response"]["body_decoded"] == '{"answer":"world"}'
    explanation = payload(app.handle("GET", "/api/cassettes/demo/explain?flow=1"))
    assert explanation["pre_hash"].startswith("POST\napi.example.test\n/v1/messages\na=1&b=2\n")
    assert "§VOLATILE§" in explanation["canonical_body"]
    assert payload(app.handle("GET", "/api/cassettes/demo/mismatch"))["diff"] == (
        "- recorded\n+ live\n"
    )
    assert payload(app.handle("GET", "/api/cassettes/demo/diff"))["kind"] == ("mismatch")
    observation = payload(app.handle("GET", "/api/cassettes/demo/observation"))
    assert observation["workspace"]["sha256"]
    assert observation["process"]["exit_code"] == 0


def test_fork_result_and_diff_routes_prefer_hybrid_result(tmp_path):
    root = tmp_path / "cassettes"
    cassette = make_cassette(root)
    result = {
        "exit_code": 2,
        "downstream": {"matches": False, "tool_calls": {"matches": True}},
    }
    (cassette / "fork-result.json").write_text(json.dumps(result), encoding="utf-8")
    app = UIApp(root, static_dir=tmp_path)

    assert payload(app.handle("GET", "/api/cassettes/demo/fork-result")) == result
    assert payload(app.handle("GET", "/api/cassettes/demo/diff")) == {
        "kind": "fork",
        "exit_code": 2,
        "downstream": result["downstream"],
    }


def test_static_assets_are_contained_and_support_spa_fallback(tmp_path):
    root = tmp_path / "cassettes"
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
    (static / "app.js").write_text("console.log('ok')", encoding="utf-8")
    app = UIApp(root, static_dir=static)

    index = app.handle("GET", "/runs/demo")
    assert index.status == 200
    assert index.body == b"<h1>dashboard</h1>"
    script = app.handle("GET", "/app.js")
    assert script.headers["content-type"] in {
        "text/javascript",
        "application/javascript",
    }
    assert app.handle("GET", "/../pyproject.toml").status == 404
    assert app.handle("POST", "/app.js").status == 405


def test_write_routes_require_explicit_flag_and_local_json_origin(tmp_path):
    root = tmp_path / "cassettes"
    make_cassette(root)
    disabled = UIApp(root, static_dir=tmp_path)
    request_headers = {
        "content-type": "application/json",
        "origin": "http://127.0.0.1:8765",
        "host": "127.0.0.1:8765",
    }

    assert (
        disabled.handle(
            "POST",
            "/api/cassettes/demo/replay",
            headers=request_headers,
            body=b"{}",
        ).status
        == 403
    )
    enabled = UIApp(root, static_dir=tmp_path, allow_write=True)
    assert (
        enabled.handle(
            "POST",
            "/api/cassettes/demo/replay",
            headers={**request_headers, "origin": "https://evil.example"},
            body=b"{}",
        ).status
        == 403
    )
    assert (
        enabled.handle(
            "POST",
            "/api/cassettes/demo/replay",
            headers={**request_headers, "host": "evil.example"},
            body=b"{}",
        ).status
        == 403
    )
    assert (
        enabled.handle(
            "POST",
            "/api/cassettes/demo/replay",
            headers={"content-type": "text/plain"},
            body=b"{}",
        ).status
        == 415
    )
    assert (
        enabled.handle(
            "POST",
            "/api/cassettes/demo/replay",
            headers=request_headers,
            body=b"x" * (MAX_REQUEST_BODY + 1),
        ).status
        == 413
    )


def test_write_replay_and_fork_validate_and_dispatch(tmp_path):
    root = tmp_path / "cassettes"
    cassette = make_cassette(root)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        artifact = "fork-result.json" if kwargs.get("fork_at") is not None else "last-replay.json"
        (cassette / artifact).write_text(json.dumps({"exit_code": 0}), encoding="utf-8")
        return ExitCode.SUCCESS

    app = UIApp(
        root,
        static_dir=tmp_path,
        allow_write=True,
        replay_executor=execute,
    )
    headers = {
        "content-type": "application/json",
        "host": "localhost:8765",
    }

    replay = app.handle(
        "POST",
        "/api/cassettes/demo/replay",
        headers=headers,
        body=b'{"strict":true}',
    )
    fork = app.handle(
        "POST",
        "/api/cassettes/demo/fork",
        headers=headers,
        body=b'{"fork_at":1,"env_file":"/tmp/demo.env"}',
    )

    assert replay.status == 200
    assert fork.status == 200
    assert calls == [
        {"cassette": cassette, "strict": True},
        {
            "cassette": cassette,
            "strict": False,
            "fork_at": 1,
            "env_file": Path("/tmp/demo.env"),
        },
    ]
    assert (
        app.handle(
            "POST",
            "/api/cassettes/demo/fork",
            headers=headers,
            body=b'{"fork_at":true}',
        ).status
        == 400
    )


def test_path_traversal_and_existing_baseline_are_rejected(tmp_path):
    root = tmp_path / "cassettes"
    make_cassette(root)
    app = UIApp(root, static_dir=tmp_path, allow_write=True)
    headers = {"content-type": "application/json", "host": "127.0.0.1"}

    assert app.handle("GET", "/api/cassettes/%2e%2e/timeline").status == 400
    response = app.handle(
        "POST",
        "/api/cassettes/demo/accept",
        headers=headers,
        body=b'{"destination":"demo"}',
    )
    assert response.status == 409


def test_accept_records_to_hidden_staging_then_publishes_new_baseline(tmp_path):
    root = tmp_path / "cassettes"
    make_cassette(root)
    observed = {}

    def record(**kwargs):
        observed.update(kwargs)
        writer = CassetteWriter(kwargs["out"])
        writer.initialize(stub_manifest())
        return ExitCode.SUCCESS

    app = UIApp(
        root,
        static_dir=tmp_path,
        allow_write=True,
        record_executor=record,
    )
    response = app.handle(
        "POST",
        "/api/cassettes/demo/accept",
        headers={"content-type": "application/json", "host": "127.0.0.1"},
        body=b'{"destination":"demo-fresh","env_file":"/tmp/demo.env"}',
    )

    assert response.status == 201
    assert payload(response)["cassette"] == "demo-fresh"
    assert observed["image"] == "image"
    assert observed["command"] == ["workload"]
    assert observed["env_file"] == Path("/tmp/demo.env")
    assert observed["out"].name.startswith(".demo-fresh.")
    assert (root / "demo-fresh" / "manifest.json").is_file()
    assert not any(path.name.startswith(".demo-fresh.") for path in root.iterdir())


def test_http_adapter_binds_loopback_and_preserves_api_errors(tmp_path):
    root = tmp_path / "cassettes"
    make_cassette(root)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("dashboard", encoding="utf-8")
    server = create_server(
        cassette_root=root,
        static_dir=static,
        port=0,
        allow_write=False,
    )
    assert server.server_address[0] == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/api/cassettes", timeout=5) as response:
            assert response.status == 200
            assert json.load(response)["cassettes"][0]["name"] == "demo"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "connect-src 'self'" in response.headers[
                "content-security-policy"
            ]

        request = urllib.request.Request(
            f"{base}/api/cassettes/demo/replay",
            data=b"{}",
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert json.load(exc)["error"] == "write actions are disabled"
        else:
            raise AssertionError("disabled write unexpectedly succeeded")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
