"""LCS alignment for provider-neutral tool-call sequences."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from replayable.channels.providers.anthropic import ToolCall

MAX_ALIGNMENT_CELLS = 4_000_000
MAX_SEQUENCE_LENGTH = 10_000


class StructuralDiffError(ValueError):
    """Tool-call sequences cannot be compared safely."""


class OperationKind(StrEnum):
    EQUAL = "equal"
    INSERT = "insert"
    DELETE = "delete"
    SUBSTITUTE = "substitute"


@dataclass(frozen=True)
class ToolCallValue:
    """A stable display value; IDs are retained but excluded from alignment."""

    id: str | None
    name: str
    input: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "input": self.input}


@dataclass(frozen=True)
class DiffOperation:
    kind: OperationKind
    baseline_index: int | None
    candidate_index: int | None
    baseline: ToolCallValue | None
    candidate: ToolCallValue | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "baseline_index": self.baseline_index,
            "candidate_index": self.candidate_index,
            "baseline": self.baseline.as_dict() if self.baseline else None,
            "candidate": self.candidate.as_dict() if self.candidate else None,
        }


@dataclass(frozen=True)
class StructuralDiff:
    baseline_count: int
    candidate_count: int
    operations: tuple[DiffOperation, ...]

    @property
    def matches(self) -> bool:
        return all(operation.kind is OperationKind.EQUAL for operation in self.operations)

    def count(self, kind: OperationKind) -> int:
        return sum(operation.kind is kind for operation in self.operations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "structural",
            "matches": self.matches,
            "baseline_count": self.baseline_count,
            "candidate_count": self.candidate_count,
            "summary": {
                kind.value: self.count(kind)
                for kind in (
                    OperationKind.INSERT,
                    OperationKind.DELETE,
                    OperationKind.SUBSTITUTE,
                )
            },
            "operations": [operation.as_dict() for operation in self.operations],
        }


ToolCallLike = ToolCall | Mapping[str, Any]


def _tool_call(value: ToolCallLike, index: int, side: str) -> ToolCallValue:
    if isinstance(value, ToolCall):
        tool_id: object = value.id
        name: object = value.name
        tool_input: object = value.input
    elif isinstance(value, Mapping):
        tool_id = value.get("id")
        name = value.get("name")
        tool_input = value.get("input")
    else:
        raise StructuralDiffError(f"{side} tool call {index} must be an object")
    if tool_id is not None and not isinstance(tool_id, str):
        raise StructuralDiffError(f"{side} tool call {index} id must be a string or null")
    if not isinstance(name, str) or not name:
        raise StructuralDiffError(f"{side} tool call {index} name must be non-empty")
    if not isinstance(tool_input, dict):
        raise StructuralDiffError(f"{side} tool call {index} input must be an object")
    try:
        canonical_input = json.dumps(
            tool_input,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise StructuralDiffError(f"{side} tool call {index} input is not canonical JSON") from exc
    return ToolCallValue(tool_id, name, json.loads(canonical_input))


def _signature(value: ToolCallValue) -> tuple[str, str]:
    return (
        value.name,
        json.dumps(
            value.input,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _lcs_lengths(
    left: Sequence[tuple[str, str]],
    right: Sequence[tuple[str, str]],
) -> list[int]:
    previous = [0] * (len(right) + 1)
    for left_value in left:
        current = [0]
        for index, right_value in enumerate(right, start=1):
            if left_value == right_value:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous


def _lcs_pairs(
    baseline: Sequence[tuple[str, str]],
    candidate: Sequence[tuple[str, str]],
    baseline_offset: int = 0,
    candidate_offset: int = 0,
) -> list[tuple[int, int]]:
    """Hirschberg LCS: exact alignment with linear auxiliary memory."""

    if not baseline or not candidate:
        return []
    if len(baseline) == 1:
        try:
            candidate_index = candidate.index(baseline[0])
        except ValueError:
            return []
        return [(baseline_offset, candidate_offset + candidate_index)]

    midpoint = len(baseline) // 2
    forward = _lcs_lengths(baseline[:midpoint], candidate)
    backward = _lcs_lengths(
        baseline[midpoint:][::-1],
        candidate[::-1],
    )
    scores = [
        forward[index] + backward[len(candidate) - index]
        for index in range(len(candidate) + 1)
    ]
    split = max(range(len(candidate) + 1), key=scores.__getitem__)
    del forward, backward
    return _lcs_pairs(
        baseline[:midpoint],
        candidate[:split],
        baseline_offset,
        candidate_offset,
    ) + _lcs_pairs(
        baseline[midpoint:],
        candidate[split:],
        baseline_offset + midpoint,
        candidate_offset + split,
    )


def _changed_segment(
    baseline: tuple[ToolCallValue, ...],
    candidate: tuple[ToolCallValue, ...],
    baseline_start: int,
    baseline_end: int,
    candidate_start: int,
    candidate_end: int,
) -> list[DiffOperation]:
    deleted = baseline_end - baseline_start
    inserted = candidate_end - candidate_start
    substitutions = min(deleted, inserted)
    operations = [
        DiffOperation(
            kind=OperationKind.SUBSTITUTE,
            baseline_index=baseline_start + offset,
            candidate_index=candidate_start + offset,
            baseline=baseline[baseline_start + offset],
            candidate=candidate[candidate_start + offset],
        )
        for offset in range(substitutions)
    ]
    operations.extend(
        DiffOperation(
            kind=OperationKind.DELETE,
            baseline_index=index,
            candidate_index=None,
            baseline=baseline[index],
            candidate=None,
        )
        for index in range(baseline_start + substitutions, baseline_end)
    )
    operations.extend(
        DiffOperation(
            kind=OperationKind.INSERT,
            baseline_index=None,
            candidate_index=index,
            baseline=None,
            candidate=candidate[index],
        )
        for index in range(candidate_start + substitutions, candidate_end)
    )
    return operations


def diff_tool_calls(
    baseline: Sequence[ToolCallLike],
    candidate: Sequence[ToolCallLike],
) -> StructuralDiff:
    """Align two tool sequences and classify localized structural changes."""

    if len(baseline) > MAX_SEQUENCE_LENGTH or len(candidate) > MAX_SEQUENCE_LENGTH:
        raise StructuralDiffError(
            "tool-call sequence is too long; "
            f"each side is limited to {MAX_SEQUENCE_LENGTH} calls"
        )
    if len(baseline) * len(candidate) > MAX_ALIGNMENT_CELLS:
        raise StructuralDiffError(
            "tool-call alignment is too large; "
            f"{len(baseline)} x {len(candidate)} exceeds {MAX_ALIGNMENT_CELLS} cells"
        )
    baseline_values = tuple(
        _tool_call(value, index, "baseline") for index, value in enumerate(baseline)
    )
    candidate_values = tuple(
        _tool_call(value, index, "candidate") for index, value in enumerate(candidate)
    )
    pairs = _lcs_pairs(
        tuple(_signature(value) for value in baseline_values),
        tuple(_signature(value) for value in candidate_values),
    )

    operations: list[DiffOperation] = []
    baseline_cursor = 0
    candidate_cursor = 0
    for baseline_index, candidate_index in pairs:
        operations.extend(
            _changed_segment(
                baseline_values,
                candidate_values,
                baseline_cursor,
                baseline_index,
                candidate_cursor,
                candidate_index,
            )
        )
        operations.append(
            DiffOperation(
                kind=OperationKind.EQUAL,
                baseline_index=baseline_index,
                candidate_index=candidate_index,
                baseline=baseline_values[baseline_index],
                candidate=candidate_values[candidate_index],
            )
        )
        baseline_cursor = baseline_index + 1
        candidate_cursor = candidate_index + 1
    operations.extend(
        _changed_segment(
            baseline_values,
            candidate_values,
            baseline_cursor,
            len(baseline_values),
            candidate_cursor,
            len(candidate_values),
        )
    )
    return StructuralDiff(
        baseline_count=len(baseline_values),
        candidate_count=len(candidate_values),
        operations=tuple(operations),
    )


class StructuralToolDiffer:
    """`Differ` implementation for observation tool-call sequences."""

    def diff(
        self,
        baseline: Sequence[ToolCallLike],
        candidate: Sequence[ToolCallLike],
    ) -> StructuralDiff:
        return diff_tool_calls(baseline, candidate)
