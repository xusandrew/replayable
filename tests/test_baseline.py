from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest
from conftest import stub_manifest
from typer.testing import CliRunner

import replayable.baseline
import replayable.cli
from replayable.baseline import BaselineError, prepare_baseline
from replayable.cassette import CassetteWriter
from replayable.cli import app
from replayable.exit_codes import ExitCode
from replayable.snapshot import create_snapshot
from replayable.verdict.differ_structural import StructuralDiffError

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
runner = CliRunner()


def write_cassette(path: Path, *, stdout: str, workspace_text: str) -> None:
    writer = CassetteWriter(path)
    writer.initialize(stub_manifest())
    with tempfile.TemporaryDirectory(dir=path.parent) as workspace_name:
        workspace = Path(workspace_name)
        (workspace / "result.txt").write_text(workspace_text, encoding="utf-8")
        snapshot = create_snapshot(workspace, path)
    stdout_bytes = stdout.encode()
    (path / "agent.stdout").write_bytes(stdout_bytes)
    (path / "agent.stderr").write_bytes(b"")
    writer.update_manifest(
        flow_count=0,
        event_count=0,
        workspace_sha256=snapshot.sha256,
        stdout_sha256=hashlib.sha256(stdout_bytes).hexdigest(),
        stderr_sha256=EMPTY_SHA256,
        record_exit_code=0,
        record_wall_time_seconds=1.0,
    )


def candidate_recorder(**kwargs) -> ExitCode:
    write_cassette(
        kwargs["out"],
        stdout="new output\n",
        workspace_text="new workspace\n",
    )
    return ExitCode.SUCCESS


def test_prepare_renders_diff_and_atomically_replaces_baseline(tmp_path):
    cassette = tmp_path / "demo"
    write_cassette(cassette, stdout="old output\n", workspace_text="old workspace\n")

    with prepare_baseline(
        source=cassette,
        destination=cassette,
        env_file=None,
        record_executor=candidate_recorder,
    ) as candidate:
        assert "old output" in candidate.preview
        assert "new output" in candidate.preview
        assert (cassette / "agent.stdout").read_text() == "old output\n"
        assert candidate.staging.name.startswith(".demo.candidate.")
        candidate.publish(replace=True)

    assert (cassette / "agent.stdout").read_text() == "new output\n"
    assert not any(path.name.startswith(".demo.") for path in tmp_path.iterdir())


def test_prepare_failure_preserves_baseline_and_cleans_staging(tmp_path):
    cassette = tmp_path / "demo"
    write_cassette(cassette, stdout="old output\n", workspace_text="old workspace\n")

    with pytest.raises(BaselineError, match="candidate recording exited 1"):
        with prepare_baseline(
            source=cassette,
            destination=cassette,
            env_file=None,
            record_executor=lambda **_kwargs: ExitCode.AGENT_FAILED,
        ):
            raise AssertionError("failed recording must not yield a candidate")

    assert (cassette / "agent.stdout").read_text() == "old output\n"
    assert not any(path.name.startswith(".demo.") for path in tmp_path.iterdir())


def test_unreviewable_candidate_preserves_baseline_and_cleans_staging(
    tmp_path, monkeypatch
):
    cassette = tmp_path / "demo"
    write_cassette(cassette, stdout="old output\n", workspace_text="old workspace\n")

    def reject_diff(*_args, **_kwargs):
        raise StructuralDiffError("comparison is too large")

    monkeypatch.setattr(replayable.baseline, "diff_tool_calls", reject_diff)

    with pytest.raises(BaselineError, match="cannot be compared safely"):
        with prepare_baseline(
            source=cassette,
            destination=cassette,
            env_file=None,
            record_executor=candidate_recorder,
        ):
            raise AssertionError("unreviewable recording must not yield a candidate")

    assert (cassette / "agent.stdout").read_text() == "old output\n"
    assert not any(path.name.startswith(".demo.") for path in tmp_path.iterdir())


def test_publish_rolls_back_when_candidate_rename_fails(tmp_path, monkeypatch):
    cassette = tmp_path / "demo"
    write_cassette(cassette, stdout="old output\n", workspace_text="old workspace\n")
    real_replace = replayable.baseline.os.replace

    with prepare_baseline(
        source=cassette,
        destination=cassette,
        env_file=None,
        record_executor=candidate_recorder,
    ) as candidate:

        def fail_candidate(source, destination):
            if Path(source) == candidate.staging:
                raise OSError("injected rename failure")
            return real_replace(source, destination)

        monkeypatch.setattr(replayable.baseline.os, "replace", fail_candidate)
        with pytest.raises(BaselineError, match="cannot publish baseline atomically"):
            candidate.publish(replace=True)

    assert (cassette / "agent.stdout").read_text() == "old output\n"


def test_publish_preserves_backup_when_candidate_and_rollback_renames_fail(
    tmp_path, monkeypatch
):
    cassette = tmp_path / "demo"
    write_cassette(cassette, stdout="old output\n", workspace_text="old workspace\n")
    real_replace = replayable.baseline.os.replace

    with prepare_baseline(
        source=cassette,
        destination=cassette,
        env_file=None,
        record_executor=candidate_recorder,
    ) as candidate:
        calls = 0

        def fail_publication_and_rollback(source, destination):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise OSError(f"injected rename failure {calls}")
            return real_replace(source, destination)

        monkeypatch.setattr(
            replayable.baseline.os,
            "replace",
            fail_publication_and_rollback,
        )
        with pytest.raises(
            BaselineError,
            match=r"original preserved at (?P<backup>.+)",
        ) as error:
            candidate.publish(replace=True)

    backup = Path(error.value.args[0].split("original preserved at ", 1)[1])
    assert not cassette.exists()
    assert (backup / "agent.stdout").read_text() == "old output\n"


@pytest.mark.parametrize(
    ("stdin", "expected"),
    [
        ("n\n", "old output\n"),
        ("y\n", "new output\n"),
    ],
)
def test_accept_cli_requires_confirmation(tmp_path, monkeypatch, stdin, expected):
    cassette = tmp_path / "demo"
    write_cassette(cassette, stdout="old output\n", workspace_text="old workspace\n")
    monkeypatch.setattr(replayable.cli, "record_run", candidate_recorder)

    result = runner.invoke(
        app,
        ["accept", "--cassette", str(cassette)],
        input=stdin,
    )

    assert result.exit_code == ExitCode.SUCCESS
    assert (cassette / "agent.stdout").read_text() == expected
    assert "Baseline candidate" in result.output
    if stdin == "n\n":
        assert "Baseline unchanged" in result.output
    else:
        assert "Accepted replacement baseline" in result.output
