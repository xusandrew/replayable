"""Regression coverage for replaying cassettes older than two days."""

from __future__ import annotations

import json
import os

import pytest
from fixtures.corpus import copy_fixture_cassette

from replayable.exit_codes import ExitCode


@pytest.mark.e2e
def test_old_https_cassette_replays_with_leaf_certificate_valid_at_t0(tmp_path):
    """Backdating only the CA must not hide mitmproxy's fresh-leaf time bomb."""

    if os.environ.get("REPLAYABLE_RUN_E2E") != "1":
        pytest.skip("set REPLAYABLE_RUN_E2E=1 to run Docker acceptance tests")

    from replayable.runner import default_ca_path, replay_run

    cassette = copy_fixture_cassette("curl-demo", tmp_path)

    exit_code = replay_run(
        cassette=cassette,
        strict=True,
        ca_path=default_ca_path(),
        allow_image_mismatch=True,
    )

    assert exit_code == ExitCode.SUCCESS
    state = json.loads((cassette / "replay-state.json").read_text(encoding="utf-8"))
    assert state["unconsumed_sequences"] == []
    assert not (cassette / "replay-report.json").exists()
