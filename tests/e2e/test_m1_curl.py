from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from replayable.cassette import CassetteReader, CassetteWriter
from replayable.cassette.events import EventLogReader, derive_events_from_flows
from replayable.exit_codes import ExitCode
from replayable.inspection import inspect_cassette
from replayable.runner import default_ca_path, record_run, replay_run

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("REPLAYABLE_RUN_E2E") != "1",
        reason="set REPLAYABLE_RUN_E2E=1 after building the base image",
    ),
]


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_record_replay_and_missing_flow(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert default_ca_path().is_file()
    cassette = tmp_path / "cassette"

    record_result = record_run(
        image="replayable/agent-base:local",
        command=["replayable-curl-workload"],
        out=cassette,
        port=available_port(),
    )
    record_output = capfd.readouterr().out

    assert record_result == ExitCode.SUCCESS
    reader = CassetteReader(cassette)
    manifest = reader.load_manifest()
    records = reader.load_flows().flows
    assert manifest["cassette_version"] == "2.0"
    assert manifest["flow_count"] == 4
    assert manifest["event_count"] == 4
    assert manifest["image"]["ref"] == "replayable/agent-base:local"
    assert len(records) == 4
    assert len(EventLogReader(cassette).load_events()) == 4
    assert [
        (record["key"]["host"], record["key"]["path"]) for record in records
    ] == [
        ("api.github.com", "/zen"),
        ("api.github.com", "/zen"),
        ("api.github.com", "/zen"),
        ("httpbin.org", "/post"),
    ]
    assert "SEQ  METHOD  ENDPOINT" in inspect_cassette(cassette)

    replay_result = replay_run(cassette=cassette, port=available_port())
    replay_output = capfd.readouterr().out
    assert replay_result == ExitCode.SUCCESS
    assert replay_output == record_output + (
        "DETERMINISTIC ✓ (workspace sha256 matches)\n"
    )
    assert (cassette / "agent.stdout").read_bytes() == (
        cassette / "replay-agent.stdout"
    ).read_bytes()

    remaining_records = records[1:]
    (cassette / "flows.jsonl").write_text(
        "\n".join(
            json.dumps(record, separators=(",", ":")) for record in remaining_records
        )
        + "\n",
        encoding="utf-8",
    )
    writer = CassetteWriter(cassette)
    writer.event_path.write_text("", encoding="utf-8")
    for event in derive_events_from_flows(remaining_records):
        writer.append_event(event)
    writer.update_manifest(
        flow_count=len(remaining_records),
        event_count=len(remaining_records),
    )
    missing_result = replay_run(cassette=cassette, port=available_port())
    assert missing_result == ExitCode.REPLAY_MISMATCH
