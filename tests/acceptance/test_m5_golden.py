"""The golden acceptance bar: the recorded research agent replays byte-identically.

This is the regression gate for every refactor that follows. The V1 build
restructures ``runner.py`` into ``core/``, introduces a cassette v2 event log, a
policy engine and a verdict engine. Each of those changes is only safe if this
file stays green, because these hashes are the whole claim: *a recorded agent
run reproduces exactly, offline, for $0.00*.

Three tiers, so the bar means the same thing everywhere it runs:

* offline structural checks       — always run, no Docker, no network
* replay determinism (``[E]``)    — ``REPLAYABLE_RUN_E2E=1``, needs Docker + the CA
* strict image identity           — ``REPLAYABLE_STRICT_IMAGE=1``, needs the
  exact recorded image locally

See ``tests/acceptance/README.md`` for why the third tier is opt-in.
"""

from __future__ import annotations

import json
import os

import pytest
from fixtures.corpus import copy_fixture_cassette, fixture_cassette

from replayable.cassette import CassetteReader
from replayable.exit_codes import ExitCode
from replayable.matcher import (
    RequestMatcher,
    normalize_request,
    raw_request_from_record,
)
from replayable.normalize_rules import load_rules

CASSETTE_NAME = "research-agent"

# Recorded 2026-07-29 from demo/research_agent against claude-haiku-4-5.
# These four values ARE the acceptance criterion. If a change moves any of
# them, that change altered replay behaviour — which is either a bug or a
# deliberate decision that needs this file updated in the same commit.
GOLDEN_WORKSPACE_SHA256 = (
    "72bd67c68b499f79ee72551e98acc72f2c01cedc94bf7a02d9ba99e2d561295d"
)
GOLDEN_STDOUT_SHA256 = (
    "e8ddc3c0083eeb2763e864e1cc895925e851e0c715e8f1e1890aab0172a5aa60"
)
GOLDEN_IMAGE_ID = (
    "sha256:cd398ef53ea720e85cb9c90a032db433049d7a113109bc7de7b040912d080999"
)
GOLDEN_FLOW_COUNT = 20


def _requires_docker() -> None:
    if os.environ.get("REPLAYABLE_RUN_E2E") != "1":
        pytest.skip("set REPLAYABLE_RUN_E2E=1 to run Docker acceptance tests")


# --------------------------------------------------------------------------
# Tier 1 — offline structural checks. No Docker, no network, always run.
# --------------------------------------------------------------------------


def test_golden_manifest_pins_the_recorded_identity():
    """The manifest still declares the run these hashes came from."""

    manifest = CassetteReader(fixture_cassette(CASSETTE_NAME)).load_manifest()

    assert manifest["cassette_version"] == "1.0"
    assert manifest["flow_count"] == GOLDEN_FLOW_COUNT
    assert manifest["workspace_sha256"] == GOLDEN_WORKSPACE_SHA256
    assert manifest["stdout_sha256"] == GOLDEN_STDOUT_SHA256
    assert manifest["image"]["id"] == GOLDEN_IMAGE_ID
    # A cassette without a pinned ruleset would silently re-normalize under
    # whatever rules happen to be current, which would make the bar meaningless.
    assert manifest["ruleset_version"].startswith("sha256:")


def test_golden_cassette_carries_no_unredacted_secret():
    """The recorded API key never reached disk.

    The bundle records ``env_names`` (so replay can supply dummies) but must
    never record a value. This is a write-time guarantee of ``redact.py``; the
    test pins it so a future change to the redaction path cannot quietly leak.
    """

    cassette = fixture_cassette(CASSETTE_NAME)
    manifest = CassetteReader(cassette).load_manifest()

    assert manifest["secret_env_names"] == ["ANTHROPIC_API_KEY"]
    assert manifest["nonsecret_env"] == {}

    body = (cassette / "flows.jsonl").read_text(encoding="utf-8")
    assert '"x-api-key","[REDACTED]"' in body
    assert "sk-ant-" not in body


def test_every_recorded_request_matches_itself():
    """Normalization is a fixed point over the corpus.

    Replaying a cassette means normalizing each live request and popping the
    recorded entry with the same match key. If a recorded request did not
    re-normalize to its own key, replay could never serve it — so this is the
    cheapest possible proof that the matcher and this cassette agree, and it
    catches ruleset regressions without needing Docker.
    """

    cassette = fixture_cassette(CASSETTE_NAME)
    reader = CassetteReader(cassette)
    flows = reader.load_flows().flows
    rules = load_rules(None)

    assert len(flows) == GOLDEN_FLOW_COUNT
    assert not reader.load_flows().dropped_truncated_final_line

    matcher = RequestMatcher.from_flows(flows, reader, rules)
    for flow in flows:
        raw = raw_request_from_record(flow, reader)
        served = matcher.match(raw)
        assert served["seq"] == flow["seq"], (
            f"flow {flow['seq']} did not match itself in FIFO order; "
            "the normalization ruleset and this cassette have diverged"
        )

    assert matcher.unconsumed_sequences() == []


def test_recorded_requests_have_stable_normalization():
    """Normalizing twice yields the same key — no clock or randomness leaks in."""

    cassette = fixture_cassette(CASSETTE_NAME)
    reader = CassetteReader(cassette)
    rules = load_rules(None)

    for flow in reader.load_flows().flows:
        raw = raw_request_from_record(flow, reader)
        first = normalize_request(raw, rules)
        second = normalize_request(raw, rules)
        assert first.match_key == second.match_key


def test_workspace_manifest_matches_the_golden_hash():
    """The recorded file list is the one the golden workspace hash covers."""

    cassette = fixture_cassette(CASSETTE_NAME)
    files = json.loads((cassette / "workspace.files.json").read_text(encoding="utf-8"))

    assert [entry["path"] for entry in files] == ["report.md", "sources.json"]
    recorded = (cassette / "workspace.sha256").read_text(encoding="utf-8").strip()
    assert recorded == GOLDEN_WORKSPACE_SHA256


# --------------------------------------------------------------------------
# Tier 2 — the real thing. Replay the cassette in Docker and compare hashes.
# --------------------------------------------------------------------------


@pytest.mark.e2e
def test_golden_replay_is_byte_identical(tmp_path):
    """Replay the recorded run offline and reproduce both hashes exactly.

    This is the M5 acceptance criterion and the single most valuable test in
    the repository. It needs no API key: the whole point is that replay never
    talks to Anthropic.
    """

    _requires_docker()

    from replayable.runner import default_ca_path, replay_run

    cassette = copy_fixture_cassette(CASSETTE_NAME, tmp_path)

    exit_code = replay_run(
        cassette=cassette,
        strict=True,
        ca_path=default_ca_path(),
        # CI rebuilds the image, so its ID legitimately differs there. Strict
        # identity is asserted separately in tier 3.
        allow_image_mismatch=os.environ.get("REPLAYABLE_STRICT_IMAGE") != "1",
    )

    assert exit_code == ExitCode.SUCCESS

    replay = json.loads((cassette / "last-replay.json").read_text(encoding="utf-8"))
    assert replay["workspace_sha256"] == GOLDEN_WORKSPACE_SHA256
    assert replay["stdout_sha256"] == GOLDEN_STDOUT_SHA256
    assert replay["workspace_diff"] is None
    assert replay["exit_code"] == 0

    state = json.loads((cassette / "replay-state.json").read_text(encoding="utf-8"))
    assert state["unconsumed_sequences"] == [], (
        "strict replay left recorded flows unserved; the agent took a "
        "different path through the cassette"
    )


@pytest.mark.e2e
def test_golden_replay_is_offline_and_free(tmp_path):
    """Replay produces no mismatch report and needs no credentials.

    ``replay-report.json`` is only written on mismatch, so its absence is the
    assertion that every request was served from the cassette rather than
    forwarded upstream.
    """

    _requires_docker()

    from replayable.runner import default_ca_path, replay_run

    cassette = copy_fixture_cassette(CASSETTE_NAME, tmp_path)

    assert replay_run(
        cassette=cassette,
        strict=True,
        ca_path=default_ca_path(),
        allow_image_mismatch=os.environ.get("REPLAYABLE_STRICT_IMAGE") != "1",
    ) == ExitCode.SUCCESS

    assert not (cassette / "replay-report.json").exists()


# --------------------------------------------------------------------------
# Tier 3 — strict image identity. Only on a host holding the recorded image.
# --------------------------------------------------------------------------


@pytest.mark.e2e
def test_golden_replay_under_strict_image_identity(tmp_path):
    """The strongest form of the claim: same image ID, same bytes out.

    Skipped wherever the recorded image is absent (CI rebuilds it, so its ID
    differs by construction). Where it does run, it removes the last degree of
    freedom from the determinism claim.
    """

    _requires_docker()
    if os.environ.get("REPLAYABLE_STRICT_IMAGE") != "1":
        pytest.skip(
            "set REPLAYABLE_STRICT_IMAGE=1 on a host holding the recorded image "
            f"{GOLDEN_IMAGE_ID[:19]}… to assert exact image identity"
        )

    from replayable.runner import default_ca_path, replay_run

    cassette = copy_fixture_cassette(CASSETTE_NAME, tmp_path)

    assert replay_run(
        cassette=cassette,
        strict=True,
        ca_path=default_ca_path(),
        allow_image_mismatch=False,
    ) == ExitCode.SUCCESS

    replay = json.loads((cassette / "last-replay.json").read_text(encoding="utf-8"))
    assert replay["workspace_sha256"] == GOLDEN_WORKSPACE_SHA256
    assert replay["stdout_sha256"] == GOLDEN_STDOUT_SHA256


@pytest.mark.e2e
def test_golden_replay_from_native_v2_event_log(tmp_path):
    """A materialized v2 event log must not change legacy replay behavior."""

    _requires_docker()

    from replayable.cassette import CassetteReader, CassetteWriter
    from replayable.cassette.events import EventLogReader, derive_events_from_flows
    from replayable.runner import default_ca_path, replay_run

    cassette = copy_fixture_cassette(CASSETTE_NAME, tmp_path)
    flows = CassetteReader(cassette).load_flows().flows
    writer = CassetteWriter(cassette)
    events = derive_events_from_flows(flows)
    for event in events:
        writer.append_event(event)
    writer.update_manifest(cassette_version="2.0", event_count=len(events))

    assert len(EventLogReader(cassette).load_events()) == GOLDEN_FLOW_COUNT
    assert (
        replay_run(
            cassette=cassette,
            strict=True,
            ca_path=default_ca_path(),
            allow_image_mismatch=os.environ.get("REPLAYABLE_STRICT_IMAGE") != "1",
        )
        == ExitCode.SUCCESS
    )
    replay = json.loads((cassette / "last-replay.json").read_text(encoding="utf-8"))
    assert replay["workspace_sha256"] == GOLDEN_WORKSPACE_SHA256
    assert replay["stdout_sha256"] == GOLDEN_STDOUT_SHA256
