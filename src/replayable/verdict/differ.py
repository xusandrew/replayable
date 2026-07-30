"""Small, typed seams shared by hash and structural verdicts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

BaselineT = TypeVar("BaselineT", contravariant=True)
CandidateT = TypeVar("CandidateT", contravariant=True)
ResultT = TypeVar("ResultT", covariant=True)


class Differ(Protocol[BaselineT, CandidateT, ResultT]):
    """A deterministic comparison strategy."""

    def diff(self, baseline: BaselineT, candidate: CandidateT) -> ResultT:
        """Compare candidate behavior with its baseline."""


@dataclass(frozen=True)
class HashDiff:
    """Comparison of two already-computed SHA-256 identities."""

    baseline_sha256: str
    candidate_sha256: str
    matches: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "kind": "hash",
            "baseline_sha256": self.baseline_sha256,
            "candidate_sha256": self.candidate_sha256,
            "matches": self.matches,
        }


class HashDiffer:
    """Compare validated bare or `sha256:`-prefixed SHA-256 strings."""

    def diff(self, baseline: str, candidate: str) -> HashDiff:
        baseline_digest = _validate_sha256(baseline, "baseline")
        candidate_digest = _validate_sha256(candidate, "candidate")
        return HashDiff(
            baseline_sha256=baseline,
            candidate_sha256=candidate,
            matches=baseline_digest == candidate_digest,
        )


def _validate_sha256(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{location} SHA-256 must be a string")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{location} SHA-256 is invalid")
    return digest
