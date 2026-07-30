"""Deterministic downstream similarity for hybrid replay verdicts."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from replayable.verdict.differ_structural import (
    OperationKind,
    StructuralDiff,
    diff_tool_calls,
)
from replayable.verdict.observation import Observation

DEFAULT_SIMILARITY_THRESHOLD = 0.85
TOKEN_PATTERN = re.compile(r"[\w]+(?:['\u2019-][\w]+)*", re.UNICODE)


class SimilarityError(ValueError):
    """Similarity inputs or configuration are not trustworthy."""


def lexical_similarity(baseline: str, candidate: str) -> float:
    """Multiset Sorensen-Dice score over case-folded lexical tokens."""

    left = Counter(token.casefold() for token in TOKEN_PATTERN.findall(baseline))
    right = Counter(token.casefold() for token in TOKEN_PATTERN.findall(candidate))
    total = sum(left.values()) + sum(right.values())
    if total == 0:
        return 1.0
    overlap = sum((left & right).values())
    return 2 * overlap / total


def tool_similarity(diff: StructuralDiff) -> float:
    denominator = max(diff.baseline_count, diff.candidate_count)
    if denominator == 0:
        return 1.0
    equal = diff.count(OperationKind.EQUAL)
    return equal / denominator


def output_file_similarity(
    baseline: tuple[dict[str, Any], ...],
    candidate: tuple[dict[str, Any], ...],
) -> float:
    """Exact-content overlap across non-directory output paths."""

    def values(files: tuple[dict[str, Any], ...]) -> dict[str, tuple[Any, ...]]:
        result: dict[str, tuple[Any, ...]] = {}
        for item in files:
            if item.get("type") == "directory":
                continue
            path = item.get("path")
            digest = item.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                raise SimilarityError("workspace file entries require path and sha256")
            result[path] = (
                digest,
                item.get("type"),
                item.get("mode"),
            )
        return result

    left = values(baseline)
    right = values(candidate)
    paths = set(left) | set(right)
    if not paths:
        return 1.0
    equal = sum(left.get(path) == right.get(path) for path in paths)
    return equal / len(paths)


def downstream_similarity(
    baseline: Observation,
    candidate: Observation,
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    structural_diff: StructuralDiff | None = None,
) -> dict[str, Any]:
    """Blend independently inspectable lexical, tool, and file evidence."""

    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise SimilarityError("similarity threshold must be between 0 and 1")
    structural = structural_diff or diff_tool_calls(baseline.tool_calls, candidate.tool_calls)
    lexical = lexical_similarity(
        baseline.transcript.stdout,
        candidate.transcript.stdout,
    )
    tools = tool_similarity(structural)
    files = output_file_similarity(
        baseline.workspace_files,
        candidate.workspace_files,
    )
    score = 0.60 * lexical + 0.25 * tools + 0.15 * files
    serialized_score = round(score, 4)
    return {
        "kind": "lexical_structural",
        "score": serialized_score,
        "threshold": float(threshold),
        # Judge the value consumers can actually inspect. Otherwise a raw
        # 0.84996 would serialize as 0.85 while reporting below a 0.85
        # threshold.
        "passes": serialized_score >= threshold,
        "components": {
            "transcript_lexical": round(lexical, 4),
            "tool_sequence": round(tools, 4),
            "output_files": round(files, 4),
        },
        "weights": {
            "transcript_lexical": 0.60,
            "tool_sequence": 0.25,
            "output_files": 0.15,
        },
    }
