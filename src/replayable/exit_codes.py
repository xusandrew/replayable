"""Process exit codes (M0-R3), enforced from day one.

Every CLI code path must exit with one of these values so callers
(scripts, CI, tests) can branch on the outcome without parsing output.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Exit codes for the replayable CLI."""

    # The harness completed its job: record finished, or replay was deterministic.
    SUCCESS = 0

    # The agent process inside the container exited nonzero; the harness itself worked.
    AGENT_FAILED = 1

    # Replay mismatch: matcher failure (unmatched/unconsumed flows in --strict)
    # or workspace hash divergence.
    REPLAY_MISMATCH = 2

    # Harness/infrastructure error: docker or mitmproxy failures, bad arguments,
    # missing cassette, unimplemented functionality, etc.
    HARNESS_ERROR = 3
