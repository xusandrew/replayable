from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import replayable.runner
from replayable.cassette import CassetteWriter, base_manifest
from replayable.exit_codes import ExitCode
from replayable.inspection import explain_match, inspect_cassette
from replayable.normalize_rules import load_rules
from replayable.snapshot import create_snapshot
from replayable.runner import (
    CONTAINER_CA_PATH,
    FAKETIME_LIBRARY,
    HarnessError,
    _copy_stream,
    docker_command,
    proxy_process,
    record_run,
    replay_time_environment,
    replay_run,
)


def test_docker_command_has_the_m1_proxy_and_ca_contract(tmp_path):
    ca_path = tmp_path / "ca.pem"
    workspace = tmp_path / "workspace"
    env_file = tmp_path / ".env"

    command = docker_command(
        image="replayable/agent-base",
        command=["sh", "-c", "echo ok"],
        port=8765,
        ca_path=ca_path,
        run_id="abc123",
        workspace=workspace,
        env_file=env_file,
    )

    assert command[:6] == [
        "docker",
        "run",
        "--rm",
        "--name",
        "replayable-abc123",
        "--add-host=host.docker.internal:host-gateway",
    ]
    rendered = "\n".join(command)
    assert "HTTP_PROXY=http://host.docker.internal:8765" in command
    assert "HTTPS_PROXY=http://host.docker.internal:8765" in command
    assert "NO_PROXY=localhost,127.0.0.1" in command
    assert f"{ca_path}:{CONTAINER_CA_PATH}:ro" in command
    for name in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    ):
        assert f"{name}={CONTAINER_CA_PATH}" in command
    assert "PYTHONHASHSEED=0" in command
    assert f"{workspace.resolve()}:/workspace" in command
    assert str(env_file.resolve()) in command
    assert command.index(str(env_file.resolve())) < command.index(
        "HTTP_PROXY=http://host.docker.internal:8765"
    )
    assert rendered.endswith("replayable/agent-base\nsh\n-c\necho ok")


def test_replay_time_environment_pins_wall_clock_but_not_monotonic():
    environment = replay_time_environment(1_753_020_202.114)

    assert environment == {
        "LD_PRELOAD": FAKETIME_LIBRARY,
        "FAKETIME": "2025-07-20 14:03:22",
        "FAKETIME_DONT_FAKE_MONOTONIC": "1",
    }


def test_captured_stream_redacts_secret_across_read_boundaries():
    class ChunkedSource:
        def __init__(self):
            self.chunks = iter([b"prefix sec", b"ret-value suffix", b""])

        def read(self, _size):
            return next(self.chunks)

    stored = io.BytesIO()
    mirrored = io.BytesIO()
    _copy_stream(
        ChunkedSource(),
        stored,
        mirrored,
        {"API_TOKEN": "secret-value"},
    )

    assert stored.getvalue() == b"prefix [REDACTED:API_TOKEN] suffix"
    assert mirrored.getvalue() == b"prefix [REDACTED:API_TOKEN] suffix"


def test_replay_restores_nonsecret_env_and_dummies_secret_env(monkeypatch, tmp_path):
    cassette = tmp_path / "cassette"
    manifest = base_manifest(
        created_at="2026-07-14T00:00:00Z",
        t0_epoch=1_753_020_202.114,
        image_ref="image",
        image_digest="sha256:image",
        command=["workload"],
        environment_fingerprint="sha256:env",
    )
    manifest.update(
        {
            "env_names": ["ANTHROPIC_API_KEY", "MODEL"],
            "secret_env_names": ["ANTHROPIC_API_KEY"],
            "nonsecret_env": {"MODEL": "claude-haiku-4-5"},
        }
    )
    CassetteWriter(cassette).initialize(manifest)
    workspace = tmp_path / "empty"
    workspace.mkdir()
    create_snapshot(workspace, cassette)
    (cassette / "agent.stdout").write_bytes(b"")
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("test", encoding="utf-8")
    observed: list[str] = []

    class FakeProxyContext:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    def fake_proxy_process(**kwargs):
        Path(kwargs["addon_environment"]["REPLAYABLE_STATE_FILE"]).write_text(
            '{"unconsumed_sequences":[]}\n',
            encoding="utf-8",
        )
        return FakeProxyContext()

    def fake_run(command, **_kwargs):
        observed.extend(command)
        return 0

    monkeypatch.setattr(replayable.runner, "proxy_process", fake_proxy_process)
    monkeypatch.setattr(replayable.runner, "_run_container", fake_run)
    monkeypatch.setattr(
        replayable.runner,
        "_select_replay_image",
        lambda **_kwargs: "sha256:image",
    )

    assert replay_run(cassette=cassette, ca_path=ca_path) == ExitCode.SUCCESS
    assert "ANTHROPIC_API_KEY=[REDACTED:ANTHROPIC_API_KEY]" in observed
    assert "MODEL=claude-haiku-4-5" in observed
    assert "PYTHONHASHSEED=0" in observed
    assert "LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1" in observed


def test_proxy_is_terminated_when_container_work_raises(monkeypatch, tmp_path):
    addon = tmp_path / "addon.py"
    addon.write_text("", encoding="utf-8")
    port_checks = iter([False, True])
    monkeypatch.setattr(
        replayable.runner,
        "_port_is_open",
        lambda _port, _host="127.0.0.1": next(port_checks),
    )
    monkeypatch.setattr(
        replayable.runner, "_proxy_listen_host", lambda: "127.0.0.1"
    )
    monkeypatch.setattr(
        replayable.runner, "_require_executable", lambda _name, _fix: "mitmdump"
    )

    class FakeProcess:
        terminated = False
        killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert timeout == 5
            return 0

        def kill(self):
            self.killed = True

    process = FakeProcess()
    monkeypatch.setattr(
        replayable.runner.subprocess, "Popen", lambda *_args, **_kwargs: process
    )

    with pytest.raises(RuntimeError, match="container failed"):
        with proxy_process(
            addon=addon,
            port=8080,
            addon_environment={},
            log_path=tmp_path / "proxy.log",
        ):
            raise RuntimeError("container failed")

    assert process.terminated
    assert not process.killed


def test_proxy_readiness_timeout_still_terminates_process(monkeypatch, tmp_path):
    addon = tmp_path / "addon.py"
    addon.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        replayable.runner,
        "_port_is_open",
        lambda _port, _host="127.0.0.1": False,
    )
    monkeypatch.setattr(
        replayable.runner, "_proxy_listen_host", lambda: "127.0.0.1"
    )
    monkeypatch.setattr(
        replayable.runner, "_require_executable", lambda _name, _fix: "mitmdump"
    )

    class FakeProcess:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0

    process = FakeProcess()
    monkeypatch.setattr(
        replayable.runner.subprocess, "Popen", lambda *_args, **_kwargs: process
    )

    with pytest.raises(HarnessError, match="did not listen"):
        with proxy_process(
            addon=addon,
            port=8080,
            addon_environment={},
            log_path=tmp_path / "proxy.log",
            readiness_timeout_seconds=0,
        ):
            pytest.fail("proxy should not become ready")

    assert process.terminated


def test_replay_mismatch_marker_overrides_container_exit(monkeypatch, tmp_path):
    cassette = tmp_path / "cassette"
    CassetteWriter(cassette).initialize(
        base_manifest(
            created_at="2026-07-14T00:00:00Z",
            t0_epoch=0.0,
            image_ref="image",
            image_digest="sha256:image",
            command=["workload"],
            environment_fingerprint="sha256:env",
        )
    )
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("test", encoding="utf-8")

    class FakeProxyContext:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    def fake_proxy_process(**kwargs):
        Path(kwargs["addon_environment"]["REPLAYABLE_REPORT_FILE"]).write_text(
            json.dumps(
                {
                    "live_request": {"method": "GET", "path": "/missing"},
                    "nearest_candidates": [],
                    "diff": "",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return FakeProxyContext()

    monkeypatch.setattr(replayable.runner, "proxy_process", fake_proxy_process)
    monkeypatch.setattr(
        replayable.runner,
        "_select_replay_image",
        lambda **_kwargs: "sha256:image",
    )
    monkeypatch.setattr(
        replayable.runner,
        "_run_container",
        lambda _command, **_kwargs: 22,
    )

    result = replay_run(cassette=cassette, ca_path=ca_path)
    assert result == ExitCode.REPLAY_MISMATCH


def test_inspect_renders_flow_table_and_expanded_selected_body(tmp_path):
    writer = CassetteWriter(tmp_path)
    manifest = base_manifest(
        created_at="2026-07-14T00:00:00Z",
        t0_epoch=0.0,
        image_ref="image",
        image_digest="sha256:image",
        command=["workload"],
        environment_fingerprint="sha256:env",
    )
    writer.initialize(manifest)
    writer.append_flow(
        {
            "seq": 1,
            "key": {
                "method": "GET",
                "host": "example.test",
                "port": 443,
                "path": "/resource",
            },
            "request": {
                "query": "a=1",
                "headers": [],
                "body": writer.represent_body(b"request body"),
                "body_sha256": "request-sha",
            },
            "response": {
                "status": 200,
                "headers": [],
                "body": writer.represent_body(b"response body"),
                "body_sha256": "response-sha",
            },
            "timing": {"started": 0.1, "completed": 0.2},
        }
    )
    writer.update_manifest(flow_count=1)

    summary = inspect_cassette(tmp_path)
    detail = inspect_cassette(tmp_path, 1)

    assert "SEQ  METHOD  ENDPOINT  STATUS  BODY_BYTES  SSE_CHUNKS" in summary
    assert "example.test/resource?a=1" in summary
    assert '"body": "request body"' in detail
    assert '"body": "response body"' in detail


@pytest.mark.parametrize(
    ("strict", "expected"),
    [(False, ExitCode.SUCCESS), (True, ExitCode.REPLAY_MISMATCH)],
)
def test_replay_reports_unconsumed_flows(
    monkeypatch,
    tmp_path,
    capsys,
    strict,
    expected,
):
    cassette = tmp_path / "cassette"
    CassetteWriter(cassette).initialize(
        base_manifest(
            created_at="2026-07-14T00:00:00Z",
            t0_epoch=0.0,
            image_ref="image",
            image_digest="sha256:image",
            command=["workload"],
            environment_fingerprint="sha256:env",
        )
    )
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("test", encoding="utf-8")

    class FakeProxyContext:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    def fake_proxy_process(**kwargs):
        Path(kwargs["addon_environment"]["REPLAYABLE_STATE_FILE"]).write_text(
            '{"unconsumed_sequences":[2,3]}\n',
            encoding="utf-8",
        )
        return FakeProxyContext()

    monkeypatch.setattr(replayable.runner, "proxy_process", fake_proxy_process)
    monkeypatch.setattr(
        replayable.runner,
        "_select_replay_image",
        lambda **_kwargs: "sha256:image",
    )
    monkeypatch.setattr(
        replayable.runner,
        "_run_container",
        lambda _command, **_kwargs: 0,
    )

    assert replay_run(cassette=cassette, ca_path=ca_path, strict=strict) == expected
    assert "unconsumed flow(s): [2, 3]" in capsys.readouterr().err


def test_replay_rejects_ca_generated_after_recorded_clock(tmp_path):
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    not_before = datetime.now(UTC) - timedelta(days=1)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    cassette = tmp_path / "cassette"
    CassetteWriter(cassette).initialize(
        base_manifest(
            created_at="2026-07-14T00:00:00Z",
            t0_epoch=(not_before - timedelta(days=30)).timestamp(),
            image_ref="image",
            image_digest="sha256:image",
            command=["workload"],
            environment_fingerprint="sha256:env",
        )
    )

    with pytest.raises(HarnessError, match="generated after"):
        replay_run(cassette=cassette, ca_path=ca_path)


def test_replay_refuses_ruleset_version_mismatch(tmp_path):
    cassette = tmp_path / "cassette"
    CassetteWriter(cassette).initialize(
        base_manifest(
            created_at="2026-07-14T00:00:00Z",
            t0_epoch=0.0,
            image_ref="image",
            image_digest="sha256:image",
            command=["workload"],
            environment_fingerprint="sha256:env",
            ruleset_version="sha256:not-current",
        )
    )

    with pytest.raises(HarnessError, match="normalization rules do not match"):
        replay_run(cassette=cassette)


def test_record_pins_project_rules_in_cassette_manifest(monkeypatch, tmp_path):
    rules_path = tmp_path / "replayable.toml"
    rules_path.write_text(
        '[normalization]\nfield_names = ["custom_runtime_id"]\n',
        encoding="utf-8",
    )
    cassette = tmp_path / "cassette"
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("test", encoding="utf-8")

    class FakeProxyContext:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        replayable.runner,
        "_resolve_image_identity",
        lambda _image: ("sha256:image", "sha256:image-id"),
    )
    monkeypatch.setattr(
        replayable.runner,
        "proxy_process",
        lambda **_kwargs: FakeProxyContext(),
    )
    monkeypatch.setattr(
        replayable.runner,
        "_run_container",
        lambda _command, **_kwargs: 0,
    )

    assert (
        record_run(
            image="image",
            command=["workload"],
            out=cassette,
            ca_path=ca_path,
        )
        == ExitCode.SUCCESS
    )
    manifest = json.loads((cassette / "manifest.json").read_text(encoding="utf-8"))
    assert (cassette / "replayable.toml").read_text(encoding="utf-8") == (
        rules_path.read_text(encoding="utf-8")
    )
    assert manifest["ruleset_version"] == load_rules(rules_path).version


def test_explain_match_renders_prehash_and_normalized_body(tmp_path):
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "method": "post",
                "host": "API.EXAMPLE.COM",
                "path": "/v1/messages",
                "headers": {"content-type": "application/json"},
                "body": {
                    "tool_call_id": "dynamic-value",
                    "prompt": "hello",
                },
            }
        ),
        encoding="utf-8",
    )

    explanation = json.loads(explain_match(request_path))

    assert explanation["match_key"]
    assert explanation["pre_hash"].startswith("POST\napi.example.com\n")
    assert "§VOLATILE§" in explanation["canonical_body"]


def test_explain_match_with_cassette_ignores_cwd_rules(tmp_path, monkeypatch):
    cassette = tmp_path / "cassette"
    cassette.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    (cwd / "replayable.toml").write_text(
        '[normalization]\nfield_names = ["prompt"]\n',
        encoding="utf-8",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "method": "POST",
                "host": "api.example.com",
                "path": "/v1/messages",
                "headers": {"content-type": "application/json"},
                "body": {"prompt": "hello"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(cwd)

    with_cassette = json.loads(explain_match(request_path, cassette))
    with_cwd = json.loads(explain_match(request_path))

    # Cassette has no pinned rules → defaults keep prompt; cwd override scrubs it.
    assert '"prompt":"hello"' in with_cassette["canonical_body"]
    assert "§VOLATILE§" in with_cwd["canonical_body"]
    assert with_cassette["match_key"] != with_cwd["match_key"]
