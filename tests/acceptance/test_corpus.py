"""Guards the checked-in cassette corpus itself.

Cheap, Docker-free checks that every fixture bundle is loadable and internally
consistent. If the corpus rots, these fail before the expensive Docker
acceptance tests do — and with a clearer message.
"""

from __future__ import annotations

import subprocess

import pytest
from fixtures.corpus import CORPUS_ROOT, corpus_names, fixture_cassette

from replayable.cassette import CassetteReader

# Artifacts a replayable bundle cannot do without. Checked explicitly because
# a missing one shows up otherwise as a confusing failure deep inside replay.
REQUIRED_ARTIFACTS = ("manifest.json", "flows.jsonl")

# Additionally required to verify determinism, i.e. to compare a replayed run
# against the recording. curl-demo predates workspace snapshotting.
VERIFIABLE_ARTIFACTS = (
    "agent.stdout",
    "workspace.sha256",
    "workspace.files.json",
    "workspace.tar.gz",
)


def test_corpus_is_not_empty():
    assert corpus_names(), "the fixture cassette corpus is empty"


@pytest.mark.parametrize("name", corpus_names())
def test_fixture_has_required_artifacts(name):
    cassette = fixture_cassette(name)
    missing = [f for f in REQUIRED_ARTIFACTS if not (cassette / f).is_file()]
    assert not missing, f"fixture {name} is missing {missing}"


def test_research_agent_fixture_is_complete_enough_to_verify():
    """The golden fixture carries everything replay compares against.

    Regression guard: `.gitignore` originally carried unanchored
    `workspace.sha256` / `workspace.tar.gz` rules, which silently swallowed two
    of these files on checkout even though they were present locally. The
    cassette looked fine on the machine that created it and was broken
    everywhere else.
    """

    cassette = fixture_cassette("research-agent")
    missing = [f for f in VERIFIABLE_ARTIFACTS if not (cassette / f).is_file()]
    assert not missing, f"golden fixture is missing {missing}"


def test_no_corpus_file_is_gitignored():
    """Every checked-out corpus file is actually tracked.

    Catches the ignore-rule bug class at its source rather than as a
    FileNotFoundError in an unrelated assertion.
    """

    on_disk = sorted(
        path
        for path in CORPUS_ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    assert on_disk, "no corpus files on disk"

    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *map(str, on_disk)],
        capture_output=True,
        text=True,
        cwd=CORPUS_ROOT,
    )
    # check-ignore exits 1 when nothing matched, which is the outcome we want.
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, (
        "these corpus files match a .gitignore rule and would not survive a "
        f"fresh clone: {ignored}"
    )


@pytest.mark.parametrize("name", corpus_names())
def test_fixture_cassette_loads(name):
    """Every bundle has a valid manifest and a parseable, complete flow log."""

    reader = CassetteReader(fixture_cassette(name))
    manifest = reader.load_manifest()
    loaded = reader.load_flows()

    assert manifest["cassette_version"]
    assert not loaded.dropped_truncated_final_line, (
        f"fixture {name} has a truncated final flow record; it was checked in "
        "mid-write and is not a valid recording"
    )
    assert len(loaded.flows) == manifest["flow_count"]


@pytest.mark.parametrize("name", corpus_names())
def test_fixture_bodies_are_readable(name):
    """Every inline body decodes and every blob reference resolves to its digest.

    ``read_body`` verifies the SHA-256 of a blob against its filename, so this
    also proves no blob was corrupted or truncated by the checkout.
    """

    reader = CassetteReader(fixture_cassette(name))
    for flow in reader.load_flows().flows:
        reader.read_body(flow["request"].get("body"))
        reader.read_body(flow["response"].get("body"))
