from __future__ import annotations

import pytest

from replayable.verdict.differ import HashDiffer


def test_hash_differ_reports_match_and_mismatch():
    digest = "a" * 64
    differ = HashDiffer()

    assert differ.diff(digest, digest).matches
    assert differ.diff(f"sha256:{digest}", digest).matches
    mismatch = differ.diff(digest, "b" * 64)
    assert not mismatch.matches
    assert mismatch.as_dict() == {
        "kind": "hash",
        "baseline_sha256": digest,
        "candidate_sha256": "b" * 64,
        "matches": False,
    }


@pytest.mark.parametrize("invalid", ["", "abc", "g" * 64, True, None])
def test_hash_differ_rejects_malformed_digests(invalid):
    with pytest.raises(ValueError, match="SHA-256"):
        HashDiffer().diff("a" * 64, invalid)
