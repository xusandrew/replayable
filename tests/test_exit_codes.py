"""M0-R3: exit codes are defined and enforced by every CLI code path."""

from typer.testing import CliRunner

import replayable.cli
from replayable.cli import app
from replayable.exit_codes import ExitCode
from replayable.runner import HarnessError

runner = CliRunner()


def test_exit_code_values_are_frozen():
    assert ExitCode.SUCCESS == 0
    assert ExitCode.AGENT_FAILED == 1
    assert ExitCode.REPLAY_MISMATCH == 2
    assert ExitCode.HARNESS_ERROR == 3


def test_record_returns_runner_exit_code(monkeypatch):
    monkeypatch.setattr(replayable.cli, "record_run", lambda **_kwargs: ExitCode.SUCCESS)
    result = runner.invoke(app, ["record", "--image", "x", "--", "echo", "hi"])
    assert result.exit_code == ExitCode.SUCCESS


def test_replay_returns_runner_exit_code(monkeypatch):
    monkeypatch.setattr(
        replayable.cli, "replay_run", lambda **_kwargs: ExitCode.REPLAY_MISMATCH
    )
    result = runner.invoke(app, ["replay", "--cassette", "some-cassette"])
    assert result.exit_code == ExitCode.REPLAY_MISMATCH


def test_harness_failure_is_actionable_exit_three(monkeypatch):
    def fail(**_kwargs):
        raise HarnessError("docker unavailable; start Docker Desktop")

    monkeypatch.setattr(replayable.cli, "record_run", fail)
    result = runner.invoke(app, ["record", "--image", "x", "--", "echo", "hi"])
    assert result.exit_code == ExitCode.HARNESS_ERROR
    assert "docker unavailable; start Docker Desktop" in result.output


def test_inspect_renders_bundle(monkeypatch):
    monkeypatch.setattr(replayable.cli, "inspect_cassette", lambda *_args: "Manifest")
    result = runner.invoke(app, ["inspect", "--cassette", "some-cassette"])
    assert result.exit_code == ExitCode.SUCCESS
    assert "Manifest" in result.output


def test_inspect_missing_bundle_exits_harness_error():
    result = runner.invoke(app, ["inspect", "--cassette", "some-cassette"])
    assert result.exit_code == ExitCode.HARNESS_ERROR
