from __future__ import annotations

import json

import pytest

from replayable.cassette import (
    BLOB_THRESHOLD_BYTES,
    CassetteError,
    CassetteReader,
    CassetteVersionError,
    CassetteWriter,
    base_manifest,
    env_fingerprint,
    sha256_bytes,
)


def manifest() -> dict:
    return base_manifest(
        created_at="2026-07-14T00:00:00Z",
        t0_epoch=123.5,
        image_ref="example:latest",
        image_digest="sha256:image",
        command=["workload"],
        environment_fingerprint="sha256:environment",
    )


def test_bundle_round_trip_preserves_flow_structures(tmp_path):
    writer = CassetteWriter(tmp_path)
    writer.initialize(manifest())
    request_body = writer.represent_body(b'{"prompt":"hello"}')
    response_body = writer.represent_body(b"\x00binary\xff")
    flow = {
        "seq": 1,
        "key": {"method": "POST", "host": "api.test", "port": 443, "path": "/v1"},
        "request": {
            "query": "",
            "headers": [["content-type", "application/json"]],
            "body": request_body,
            "body_sha256": sha256_bytes(b'{"prompt":"hello"}'),
        },
        "response": {
            "status": 200,
            "headers": [["content-type", "application/octet-stream"]],
            "body": response_body,
            "body_sha256": sha256_bytes(b"\x00binary\xff"),
        },
        "timing": {"started": 0.1, "completed": 0.2},
    }
    writer.append_flow(flow)
    writer.update_manifest(flow_count=1)

    reader = CassetteReader(tmp_path)
    assert reader.load_manifest()["flow_count"] == 1
    assert reader.load_flows().flows == [flow]
    assert reader.read_body(request_body) == b'{"prompt":"hello"}'
    assert reader.read_body(response_body) == b"\x00binary\xff"


def test_blob_spill_threshold_binary_spill_and_deduplication(tmp_path):
    writer = CassetteWriter(tmp_path)
    writer.initialize(manifest())

    at_threshold = writer.represent_body(b"a" * BLOB_THRESHOLD_BYTES)
    over_threshold = writer.represent_body(b"a" * (BLOB_THRESHOLD_BYTES + 1))
    duplicate = writer.represent_body(b"a" * (BLOB_THRESHOLD_BYTES + 1))
    binary = writer.represent_body(b"\xff")

    assert "inline_utf8" in at_threshold
    assert over_threshold == duplicate
    assert over_threshold["blob"].startswith("blobs/")
    assert binary["blob"].startswith("blobs/")
    assert len(list((tmp_path / "blobs").iterdir())) == 2


def test_truncated_final_jsonl_line_is_detected_and_dropped(tmp_path):
    writer = CassetteWriter(tmp_path)
    writer.initialize(manifest())
    writer.append_flow({"seq": 1})
    with (tmp_path / "flows.jsonl").open("ab") as output:
        output.write(b'{"seq":2')

    result = CassetteReader(tmp_path).load_flows()

    assert result.flows == [{"seq": 1}]
    assert result.dropped_truncated_final_line


def test_invalid_complete_jsonl_line_is_not_silently_dropped(tmp_path):
    writer = CassetteWriter(tmp_path)
    writer.initialize(manifest())
    (tmp_path / "flows.jsonl").write_text('{"seq":1}\nnot-json\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid JSONL"):
        CassetteReader(tmp_path).load_flows()


def test_major_version_mismatch_is_rejected(tmp_path):
    incompatible = manifest()
    incompatible["cassette_version"] = "2.0"
    writer = CassetteWriter(tmp_path)
    writer.initialize(incompatible)

    with pytest.raises(CassetteVersionError, match="unsupported"):
        CassetteReader(tmp_path).load_manifest()


def test_invalid_version_string_is_rejected(tmp_path):
    invalid = manifest()
    invalid["cassette_version"] = "not-a-version"
    CassetteWriter(tmp_path).initialize(invalid)

    with pytest.raises(CassetteError, match="invalid cassette_version"):
        CassetteReader(tmp_path).load_manifest()


@pytest.mark.parametrize(
    ("raw_manifest", "message"),
    [
        ("[]", "must be an object"),
        ("{}", "missing a string cassette_version"),
        ("not-json", "unreadable"),
    ],
)
def test_malformed_manifests_are_actionable(tmp_path, raw_manifest, message):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "manifest.json").write_text(raw_manifest, encoding="utf-8")

    with pytest.raises(CassetteError, match=message):
        CassetteReader(tmp_path).load_manifest()


def test_missing_manifest_and_flow_file_are_actionable(tmp_path):
    reader = CassetteReader(tmp_path)
    with pytest.raises(CassetteError, match="manifest not found"):
        reader.load_manifest()

    CassetteWriter(tmp_path).initialize(manifest())
    (tmp_path / "flows.jsonl").unlink()
    with pytest.raises(CassetteError, match="flow file not found"):
        reader.load_flows()


def test_reinitialize_removes_stale_blobs(tmp_path):
    writer = CassetteWriter(tmp_path)
    writer.initialize(manifest())
    writer.represent_body(b"\xff")
    assert any((tmp_path / "blobs").iterdir())

    writer.initialize(manifest())

    assert not any((tmp_path / "blobs").iterdir())


def test_blank_lines_are_ignored_but_non_object_flows_are_rejected(tmp_path):
    writer = CassetteWriter(tmp_path)
    writer.initialize(manifest())
    (tmp_path / "flows.jsonl").write_text("\n  \n{}\n", encoding="utf-8")
    assert CassetteReader(tmp_path).load_flows().flows == [{}]

    (tmp_path / "flows.jsonl").write_text("[]\n", encoding="utf-8")
    with pytest.raises(CassetteError, match="must be an object"):
        CassetteReader(tmp_path).load_flows()


def test_body_reader_rejects_unsafe_missing_corrupt_and_invalid_representations(tmp_path):
    writer = CassetteWriter(tmp_path)
    writer.initialize(manifest())
    reader = CassetteReader(tmp_path)
    assert reader.read_body(None) == b""

    with pytest.raises(CassetteError, match="invalid blob path"):
        reader.read_body({"blob": "../secret"})
    with pytest.raises(CassetteError, match="unreadable"):
        reader.read_body({"blob": "blobs/does-not-exist"})
    with pytest.raises(CassetteError, match="invalid body representation"):
        reader.read_body({"unexpected": "value"})

    representation = writer.represent_body(b"\xff")
    (tmp_path / representation["blob"]).write_bytes(b"corrupt")
    with pytest.raises(CassetteError, match="digest mismatch"):
        reader.read_body(representation)


def test_environment_fingerprint_hides_secret_values():
    first = env_fingerprint(
        {"API_TOKEN": "real-first", "MODE": "test"},
        secret_names={"API_TOKEN"},
    )
    second = env_fingerprint(
        {"API_TOKEN": "different-secret", "MODE": "test"},
        secret_names={"API_TOKEN"},
    )

    assert first == second
    assert "real-first" not in first
    assert json.dumps(first)
