"""Single accessor for the checked-in cassette corpus.

Every test reaches fixture cassettes through this module rather than
hardcoding paths, so the corpus can move without a repo-wide edit.
"""

from __future__ import annotations

import shutil
from pathlib import Path

CORPUS_ROOT = Path(__file__).resolve().parent / "cassettes"

# Recording artifacts a fixture bundle is expected to carry. Replay outputs
# (``last-replay.json``, ``replay-*``) are deliberately not checked in: they are
# products of running the test, not inputs to it.
RECORDING_ARTIFACTS = (
    "manifest.json",
    "flows.jsonl",
)


def corpus_names() -> list[str]:
    """Return every fixture cassette name, sorted."""

    return sorted(path.name for path in CORPUS_ROOT.iterdir() if path.is_dir())


def fixture_cassette(name: str) -> Path:
    """Return the read-only path of a checked-in fixture cassette."""

    path = CORPUS_ROOT / name
    if not path.is_dir():
        raise FileNotFoundError(
            f"fixture cassette {name!r} not found in {CORPUS_ROOT}; "
            f"available: {', '.join(corpus_names())}"
        )
    return path


def copy_fixture_cassette(name: str, destination: Path) -> Path:
    """Copy a fixture cassette so a test can replay into it without mutating git state.

    Replay writes ``last-replay.json``, ``replay-*.log`` and friends into the
    cassette directory, so any test that actually replays must work on a copy.
    """

    source = fixture_cassette(name)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / name
    shutil.copytree(source, target)
    # Recorded bundles ship 0600 manifests and blobs; make the copy writable so
    # replay can drop its artifacts beside them.
    for path in target.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return target
