from __future__ import annotations

import json
import os
import time

import pytest

from replayable.exit_codes import ExitCode
from replayable.runner import default_ca_path, record_run, replay_run

pytestmark = pytest.mark.e2e


def test_time_workspace_transcript_and_image_are_deterministic(tmp_path):
    if os.environ.get("REPLAYABLE_RUN_E2E") != "1":
        pytest.skip("set REPLAYABLE_RUN_E2E=1 to run Docker acceptance tests")

    cassette = tmp_path / "cassette"
    source = (
        "from pathlib import Path; import time; "
        "value=repr(time.time()); "
        "Path('/workspace/time.txt').write_text(value); print(value)"
    )
    assert record_run(
        image="replayable/agent-base:local",
        command=["python", "-c", source],
        out=cassette,
        ca_path=default_ca_path(),
    ) == ExitCode.SUCCESS
    assert replay_run(
        cassette=cassette,
        strict=True,
        ca_path=default_ca_path(),
    ) == ExitCode.SUCCESS

    manifest = json.loads((cassette / "manifest.json").read_text(encoding="utf-8"))
    replay = json.loads((cassette / "last-replay.json").read_text(encoding="utf-8"))
    assert abs(float(manifest["t0_epoch"]) - time.time()) < 60
    assert manifest["workspace_sha256"] == replay["workspace_sha256"]
    assert manifest["stdout_sha256"] == replay["stdout_sha256"]
    assert (cassette / "workspace.tar.gz").is_file()
    assert (cassette / "workspace.files.json").is_file()
    assert (cassette / "run.log").is_file()
