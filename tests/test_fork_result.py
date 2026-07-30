from __future__ import annotations

import pytest

from replayable.verdict.fork_result import ForkResultError, build_fork_result
from replayable.verdict.observation import ModelSummary, Observation, Transcript
from replayable.verdict.usage import TokenUsage


def observation(
    *,
    stdout_sha256: str = "same",
    workspace_sha256: str = "workspace",
    calls: int = 0,
    cost: float = 0.0,
) -> Observation:
    return Observation(
        transcript=Transcript("", "", stdout_sha256, "stderr"),
        tool_calls=(),
        workspace_sha256=workspace_sha256,
        workspace_files=(),
        exit_code=0,
        model=ModelSummary(
            calls=calls,
            models=("claude-haiku-4-5",) if calls else (),
            usage_complete=True,
            tokens=TokenUsage(input=10 * calls, output=2 * calls),
            cost_complete=True,
            estimated_cost_usd=cost,
        ),
        wall_time_seconds=1.0,
    )


def fork_state(**updates):
    state = {
        "pinned_target": 3,
        "pinned_served": 3,
        "live_requests": 2,
        "live_responses": 2,
        "live_errors": 0,
        "live_started_epoch": 100.0,
        "live_completed_epoch": 104.5,
    }
    state.update(updates)
    return state


def test_fork_result_accounts_for_live_cost_and_downstream_change():
    result = build_fork_result(
        baseline=observation(),
        candidate=observation(stdout_sha256="changed"),
        live=observation(calls=2, cost=0.0042),
        state=fork_state(),
        captured_flow_count=2,
        wall_time_seconds=5.0,
        events=[],
    )

    assert result["segments"]["pinned"] == {
        "target_flow_count": 3,
        "served_flow_count": 3,
        "estimated_cost_usd": 0.0,
    }
    assert result["segments"]["live"]["model_calls"] == 2
    assert result["segments"]["live"]["tokens"]["input"] == 20
    assert result["segments"]["live"]["estimated_cost_usd"] == 0.0042
    assert result["segments"]["live"]["wall_time_seconds"] == 4.5
    assert result["downstream"]["matches"] is False
    assert result["downstream"]["stdout"]["matches"] is False


@pytest.mark.parametrize(
    "state",
    [
        fork_state(pinned_served=4),
        fork_state(live_requests=1),
        fork_state(live_completed_epoch=99.0),
        fork_state(live_started_epoch=None),
    ],
)
def test_fork_result_rejects_inconsistent_proxy_state(state):
    with pytest.raises(ForkResultError):
        build_fork_result(
            baseline=observation(),
            candidate=observation(),
            live=observation(),
            state=state,
            captured_flow_count=2,
            wall_time_seconds=5.0,
            events=[],
        )
