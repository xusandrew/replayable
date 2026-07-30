from __future__ import annotations

import pytest

from replayable.verdict.differ_structural import diff_tool_calls
from replayable.verdict.observation import ModelSummary, Observation, Transcript
from replayable.verdict.similarity import (
    SimilarityError,
    downstream_similarity,
    lexical_similarity,
    output_file_similarity,
    tool_similarity,
)
from replayable.verdict.usage import TokenUsage


def observation(
    text: str,
    *,
    tools=(),
    files=(),
) -> Observation:
    return Observation(
        transcript=Transcript(text, "", "stdout", "stderr"),
        tool_calls=tools,
        workspace_sha256="workspace",
        workspace_files=files,
        exit_code=0,
        model=ModelSummary(
            calls=0,
            models=(),
            usage_complete=True,
            tokens=TokenUsage(),
            cost_complete=True,
            estimated_cost_usd=0.0,
        ),
        wall_time_seconds=1.0,
    )


def test_lexical_similarity_is_casefolded_bounded_and_symmetric():
    assert lexical_similarity("", "") == 1.0
    assert lexical_similarity("Alpha beta beta", "alpha beta") == pytest.approx(0.8)
    assert lexical_similarity("alpha beta", "ALPHA beta beta") == pytest.approx(0.8)
    assert lexical_similarity("alpha", "omega") == 0.0


def test_tool_similarity_penalizes_one_insertion_without_cascade():
    baseline = [
        {"name": "search", "input": {"q": "one"}},
        {"name": "write", "input": {"path": "report.md"}},
    ]
    candidate = [
        baseline[0],
        {"name": "verify", "input": {}},
        baseline[1],
    ]

    assert tool_similarity(diff_tool_calls(baseline, candidate)) == pytest.approx(2 / 3)


def test_output_file_similarity_uses_union_and_exact_metadata():
    baseline = (
        {"path": "report.md", "sha256": "a", "type": "file", "mode": "0644"},
        {"path": "sources.json", "sha256": "b", "type": "file", "mode": "0644"},
    )
    candidate = (
        {"path": "report.md", "sha256": "a", "type": "file", "mode": "0644"},
        {"path": "sources.json", "sha256": "changed", "type": "file", "mode": "0644"},
        {"path": "extra.txt", "sha256": "c", "type": "file", "mode": "0644"},
    )

    assert output_file_similarity(baseline, candidate) == pytest.approx(1 / 3)


def test_downstream_score_exposes_components_weights_and_threshold():
    baseline = observation("final report is concise and sourced")
    candidate = observation("final report is concise and verified")

    result = downstream_similarity(baseline, candidate, threshold=0.8)

    assert result["kind"] == "lexical_structural"
    assert result["score"] == 0.9
    assert result["passes"] is True
    assert result["components"] == {
        "transcript_lexical": 0.8333,
        "tool_sequence": 1.0,
        "output_files": 1.0,
    }
    assert sum(result["weights"].values()) == 1.0


def test_downstream_threshold_uses_the_serialized_score():
    baseline = observation("a b c")
    candidate = observation("a b d e")

    result = downstream_similarity(baseline, candidate, threshold=0.7429)

    assert result["score"] == 0.7429
    assert result["passes"] is True


@pytest.mark.parametrize("threshold", [-0.1, 1.1, float("nan"), True])
def test_downstream_score_rejects_invalid_threshold(threshold):
    with pytest.raises(SimilarityError):
        downstream_similarity(observation("same"), observation("same"), threshold=threshold)
