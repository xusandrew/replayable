from __future__ import annotations

import pytest
from fixtures.corpus import fixture_cassette

import replayable.verdict.differ_structural as structural
from replayable.channels.providers.anthropic import ToolCall
from replayable.verdict.diff_render import render_structural_diff
from replayable.verdict.differ_structural import (
    OperationKind,
    StructuralDiffError,
    StructuralToolDiffer,
    diff_tool_calls,
)
from replayable.verdict.observation import build_observation


def call(name: str, value: int, *, tool_id: str | None = None) -> dict:
    return {"id": tool_id, "name": name, "input": {"value": value}}


def test_one_inserted_call_is_exactly_one_insertion_without_cascade():
    baseline = [
        call("plan", 1),
        call("search", 2),
        call("read", 3),
        call("write", 4),
    ]
    candidate = [
        call("plan", 1),
        call("search", 2),
        call("verify", 99),
        call("read", 3),
        call("write", 4),
    ]

    result = diff_tool_calls(baseline, candidate)

    assert result.count(OperationKind.INSERT) == 1
    assert result.count(OperationKind.DELETE) == 0
    assert result.count(OperationKind.SUBSTITUTE) == 0
    insertion = next(
        operation for operation in result.operations if operation.kind is OperationKind.INSERT
    )
    assert insertion.baseline_index is None
    assert insertion.candidate_index == 2
    assert insertion.candidate and insertion.candidate.name == "verify"


def test_observation_contract_aligns_fixture_with_one_inserted_call():
    baseline = build_observation(
        fixture_cassette("research-agent")
    ).tool_calls
    candidate = (
        *baseline[:6],
        ToolCall("new-id", "verify_sources", {"strict": True}),
        *baseline[6:],
    )

    result = StructuralToolDiffer().diff(baseline, candidate)

    assert result.count(OperationKind.INSERT) == 1
    assert result.count(OperationKind.SUBSTITUTE) == 0


def test_provider_generated_ids_do_not_change_structural_identity():
    baseline = [ToolCall("recorded-id", "search", {"query": "replay"})]
    candidate = [ToolCall("live-id", "search", {"query": "replay"})]

    result = StructuralToolDiffer().diff(baseline, candidate)

    assert result.matches
    assert [operation.kind for operation in result.operations] == [OperationKind.EQUAL]


def test_changed_input_is_one_substitution():
    result = diff_tool_calls(
        [call("search", 1)],
        [call("search", 2)],
    )

    assert not result.matches
    assert result.count(OperationKind.SUBSTITUTE) == 1
    assert result.count(OperationKind.INSERT) == 0
    assert result.count(OperationKind.DELETE) == 0


def test_duplicate_calls_have_deterministic_minimal_alignment():
    result = diff_tool_calls(
        [call("repeat", 1), call("middle", 2), call("repeat", 1)],
        [call("repeat", 1), call("repeat", 1)],
    )

    assert result.count(OperationKind.DELETE) == 1
    assert result.count(OperationKind.SUBSTITUTE) == 0
    deletion = next(
        operation for operation in result.operations if operation.kind is OperationKind.DELETE
    )
    assert deletion.baseline and deletion.baseline.name == "middle"


@pytest.mark.parametrize(
    ("baseline", "candidate", "insertions", "deletions"),
    [
        ([], [], 0, 0),
        ([], [call("new", 1)], 1, 0),
        ([call("old", 1)], [], 0, 1),
        (
            [call("keep", 1), call("remove", 2)],
            [call("keep", 1), call("new", 3)],
            0,
            0,
        ),
    ],
)
def test_boundaries_are_classified_without_invalid_indexes(
    baseline,
    candidate,
    insertions,
    deletions,
):
    result = diff_tool_calls(baseline, candidate)

    assert result.count(OperationKind.INSERT) == insertions
    assert result.count(OperationKind.DELETE) == deletions
    for operation in result.operations:
        if operation.baseline_index is not None:
            assert 0 <= operation.baseline_index < len(baseline)
        if operation.candidate_index is not None:
            assert 0 <= operation.candidate_index < len(candidate)


def test_mapping_key_order_is_canonicalized():
    result = diff_tool_calls(
        [{"name": "search", "input": {"a": 1, "b": 2}}],
        [{"name": "search", "input": {"b": 2, "a": 1}}],
    )

    assert result.matches


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"name": "", "input": {}}, "name"),
        ({"name": "search", "input": []}, "input"),
        ({"name": "search", "input": {"value": float("nan")}}, "canonical JSON"),
        ({"id": 123, "name": "search", "input": {}}, "id"),
    ],
)
def test_invalid_tool_calls_fail_closed(value, message):
    with pytest.raises(StructuralDiffError, match=message):
        diff_tool_calls([value], [])


def test_alignment_work_is_bounded(monkeypatch):
    monkeypatch.setattr(structural, "MAX_ALIGNMENT_CELLS", 3)

    with pytest.raises(StructuralDiffError, match="too large"):
        diff_tool_calls([call("a", 1), call("b", 2)], [call("a", 1), call("b", 2)])


def test_sequence_output_is_bounded_even_when_other_side_is_empty(monkeypatch):
    monkeypatch.setattr(structural, "MAX_SEQUENCE_LENGTH", 1)

    with pytest.raises(StructuralDiffError, match="too long"):
        diff_tool_calls([call("a", 1), call("b", 2)], [])


def test_diff_snapshot_is_not_changed_by_later_input_mutation():
    tool_input = {"nested": {"value": 1}}
    result = diff_tool_calls(
        [{"name": "search", "input": tool_input}],
        [],
    )

    tool_input["nested"]["value"] = 2

    assert result.operations[0].baseline
    assert result.operations[0].baseline.input == {"nested": {"value": 1}}


def test_serialized_diff_has_counts_and_stable_operations():
    result = diff_tool_calls(
        [call("a", 1), call("b", 2)],
        [call("a", 1), call("x", 3), call("b", 2)],
    )

    serialized = result.as_dict()

    assert serialized["matches"] is False
    assert serialized["summary"] == {
        "insert": 1,
        "delete": 0,
        "substitute": 0,
    }
    assert serialized["operations"][1]["candidate"]["name"] == "x"


def test_renderer_includes_five_events_of_context_and_omits_the_rest():
    baseline = [call(f"call-{index}", index) for index in range(13)]
    candidate = list(baseline)
    candidate[6] = call("changed", 6)
    result = diff_tool_calls(baseline, candidate)

    rendered = render_structural_diff(result)

    assert "call-1" in rendered
    assert "call-5" in rendered
    assert "changed" in rendered
    assert "call-11" in rendered
    assert "call-0 " not in rendered
    assert "call-12" not in rendered


def test_renderer_handles_matches_and_rejects_invalid_context():
    result = diff_tool_calls([call("same", 1)], [call("same", 1)])

    assert render_structural_diff(result) == "No structural differences.\n"
    with pytest.raises(ValueError, match="non-negative"):
        render_structural_diff(result, context=-1)


def test_renderer_escapes_names_and_bounds_large_call_payloads():
    result = diff_tool_calls(
        [],
        [{"name": "unsafe\nname", "input": {"value": "x" * 1_000}}],
    )

    rendered = render_structural_diff(result, context=0)

    assert "unsafe\\nname" in rendered
    assert len(rendered.splitlines()[1]) < 550
