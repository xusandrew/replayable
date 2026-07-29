"""Guards the checked-in cassette corpus itself.

Cheap, Docker-free checks that every fixture bundle is loadable and internally
consistent. If the corpus rots, these fail before the expensive Docker
acceptance tests do — and with a clearer message.
"""

from __future__ import annotations

import pytest
from fixtures.corpus import corpus_names, fixture_cassette

from replayable.cassette import CassetteReader


def test_corpus_is_not_empty():
    assert corpus_names(), "the fixture cassette corpus is empty"


@pytest.mark.parametrize("name", corpus_names())
def test_fixture_cassette_loads(name):
    """Every bundle has a valid manifest and a parseable, complete flow log."""

    reader = CassetteReader(fixture_cassette(name))
    manifest = reader.load_manifest()
    loaded = reader.load_flows()

    assert manifest["cassette_version"]
    assert not loaded.dropped_truncated_final_line, (
        f"fixture {name} has a truncated final flow record; it was checked in "
        "mid-write and is not a valid recording"
    )
    assert len(loaded.flows) == manifest["flow_count"]


@pytest.mark.parametrize("name", corpus_names())
def test_fixture_bodies_are_readable(name):
    """Every inline body decodes and every blob reference resolves to its digest.

    ``read_body`` verifies the SHA-256 of a blob against its filename, so this
    also proves no blob was corrupted or truncated by the checkout.
    """

    reader = CassetteReader(fixture_cassette(name))
    for flow in reader.load_flows().flows:
        reader.read_body(flow["request"].get("body"))
        reader.read_body(flow["response"].get("body"))
