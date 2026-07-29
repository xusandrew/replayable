"""Shared pytest configuration and fixtures.

Makes ``tests/`` importable so suites can share helpers (``fixtures.corpus``)
without each test file manipulating ``sys.path``, and provides the stubs that
``test_runner.py`` previously redefined once per test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

TESTS_ROOT = Path(__file__).resolve().parent

if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from replayable.cassette import base_manifest  # noqa: E402


class NullContext:
    """A no-op context manager standing in for ``proxy_process()``.

    Tests that exercise record/replay orchestration do not want a real
    mitmdump subprocess; they only care which arguments the orchestrator
    computed and what it did with the proxy's output files.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> bool:
        return False


@pytest.fixture
def ca_file(tmp_path: Path) -> Path:
    """A stand-in CA file.

    Contents are irrelevant to orchestration tests — only existence is
    checked. Tests that care about certificate *validity* build a real
    certificate instead (see ``test_replay_rejects_ca_generated_after_recorded_clock``).
    """

    path = tmp_path / "ca.pem"
    path.write_text("test", encoding="utf-8")
    return path


@pytest.fixture
def proxy_stub():
    """Build a ``proxy_process`` replacement that writes addon output files.

    The real proxy communicates results back to the orchestrator by writing
    JSON to paths passed in ``addon_environment``. Tests drive that contract
    directly:

        monkeypatch.setattr(
            replayable.runner,
            "proxy_process",
            proxy_stub(REPLAYABLE_STATE_FILE='{"unconsumed_sequences":[2,3]}\\n'),
        )
    """

    def build(**writes: str):
        def fake_proxy_process(**kwargs: Any) -> NullContext:
            environment = kwargs["addon_environment"]
            for env_name, payload in writes.items():
                Path(environment[env_name]).write_text(payload, encoding="utf-8")
            return NullContext()

        return fake_proxy_process

    return build


def stub_manifest(**overrides: Any) -> dict[str, Any]:
    """A minimal valid manifest for orchestration tests.

    Keyword arguments override or extend the base fields, so a test only
    states the part it actually cares about.
    """

    manifest = base_manifest(
        created_at="2026-07-14T00:00:00Z",
        t0_epoch=overrides.pop("t0_epoch", 0.0),
        image_ref="image",
        image_digest="sha256:image",
        command=["workload"],
        environment_fingerprint="sha256:env",
        ruleset_version=overrides.pop("ruleset_version", None),
    )
    manifest.update(overrides)
    return manifest
