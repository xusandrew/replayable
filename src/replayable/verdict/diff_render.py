"""Human-readable structural diff rendering with bounded context."""

from __future__ import annotations

import json

from replayable.verdict.differ_structural import (
    DiffOperation,
    OperationKind,
    StructuralDiff,
    ToolCallValue,
)

DEFAULT_CONTEXT_EVENTS = 5
MAX_RENDERED_CALL_CHARS = 500


def _ranges(diff: StructuralDiff, context: int) -> list[tuple[int, int]]:
    changed = [
        index
        for index, operation in enumerate(diff.operations)
        if operation.kind is not OperationKind.EQUAL
    ]
    if not changed:
        return []
    ranges = [
        (
            max(0, index - context),
            min(len(diff.operations), index + context + 1),
        )
        for index in changed
    ]
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _call(value: ToolCallValue | None) -> str:
    if value is None:
        return "-"
    rendered_name = json.dumps(value.name, ensure_ascii=False)
    rendered_input = json.dumps(
        value.input,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    rendered = f"{rendered_name} {rendered_input}"
    if len(rendered) > MAX_RENDERED_CALL_CHARS:
        return rendered[: MAX_RENDERED_CALL_CHARS - 3] + "..."
    return rendered


def _operation(operation: DiffOperation) -> str:
    baseline = str(operation.baseline_index) if operation.baseline_index is not None else "-"
    candidate = str(operation.candidate_index) if operation.candidate_index is not None else "-"
    if operation.kind is OperationKind.EQUAL:
        return f"  B{baseline} C{candidate} {_call(operation.baseline)}"
    if operation.kind is OperationKind.INSERT:
        return f"+ B{baseline} C{candidate} {_call(operation.candidate)}"
    if operation.kind is OperationKind.DELETE:
        return f"- B{baseline} C{candidate} {_call(operation.baseline)}"
    return f"~ B{baseline} C{candidate} {_call(operation.baseline)} -> {_call(operation.candidate)}"


def render_structural_diff(
    diff: StructuralDiff,
    *,
    context: int = DEFAULT_CONTEXT_EVENTS,
) -> str:
    """Render changed hunks with `context` aligned events on either side."""

    if isinstance(context, bool) or not isinstance(context, int) or context < 0:
        raise ValueError("diff context must be a non-negative integer")
    ranges = _ranges(diff, context)
    if not ranges:
        return "No structural differences.\n"
    lines: list[str] = []
    for start, end in ranges:
        if lines:
            lines.append("...")
        lines.append(f"@@ operations {start + 1}-{end} of {len(diff.operations)} @@")
        lines.extend(_operation(operation) for operation in diff.operations[start:end])
    return "\n".join(lines) + "\n"
