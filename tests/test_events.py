from __future__ import annotations

import json

import pytest
from fixtures.corpus import fixture_cassette

from replayable.cassette import CassetteError
from replayable.cassette.events import (
    EVENT_FILE_NAME,
    Event,
    EventChannel,
    EventKind,
    EventLogReader,
    EventLogWarning,
)


def _event_document(**updates):
    document = {
        "seq": 1,
        "lamport": 1,
        "t_rel": 0.25,
        "channel": "network",
        "kind": "http.exchange",
        "scope": "api.example.test",
        "key": "GET api.example.test:443/resource",
        "payload": {"duration_seconds": 0.5},
    }
    document.update(updates)
    return document


def test_v1_fixture_derives_complete_event_stream_without_writing():
    cassette = fixture_cassette("research-agent")
    event_path = cassette / EVENT_FILE_NAME
    assert not event_path.exists()

    events = EventLogReader(cassette).load_events()

    assert len(events) == 20
    assert [event.seq for event in events] == list(range(1, 21))
    assert [event.lamport for event in events] == list(range(1, 21))
    first = events[0]
    assert first.channel is EventChannel.NETWORK
    assert first.kind is EventKind.HTTP_EXCHANGE
    assert first.scope == "api.anthropic.com"
    assert first.key == "POST api.anthropic.com:443/v1/messages"
    assert first.t_rel == pytest.approx(0.3344559669494629)
    assert first.payload["duration_seconds"] == pytest.approx(1.9216420650482178)
    assert first.payload["flow"]["seq"] == 1
    assert not event_path.exists()


def test_native_event_round_trip_shape(tmp_path):
    event = Event.from_dict(_event_document())
    (tmp_path / EVENT_FILE_NAME).write_text(
        json.dumps(event.as_dict()) + "\n",
        encoding="utf-8",
    )

    assert EventLogReader(tmp_path).load_events() == [event]


def test_unknown_channel_and_kind_are_warned_and_skipped(tmp_path):
    documents = [
        _event_document(channel="future-channel"),
        _event_document(seq=2, lamport=2, kind="future.kind"),
        _event_document(seq=3, lamport=3),
    ]
    (tmp_path / EVENT_FILE_NAME).write_text(
        "".join(json.dumps(document) + "\n" for document in documents),
        encoding="utf-8",
    )

    with pytest.warns(EventLogWarning) as caught:
        events = EventLogReader(tmp_path).load_events()

    assert len(caught) == 2
    assert [event.seq for event in events] == [3]
    assert "future-channel" in str(caught[0].message)
    assert "future.kind" in str(caught[1].message)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"seq": True}, "seq must be an integer"),
        ({"lamport": 0}, "lamport must be an integer"),
        ({"t_rel": float("nan")}, "t_rel must be a finite"),
        ({"scope": None}, "scope must be a string"),
        ({"key": []}, "key must be a string"),
        ({"payload": []}, "payload must be an object"),
    ],
)
def test_malformed_known_event_is_rejected(tmp_path, updates, message):
    (tmp_path / EVENT_FILE_NAME).write_text(
        json.dumps(_event_document(**updates)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CassetteError, match=message):
        EventLogReader(tmp_path).load_events()


def test_truncated_final_event_is_warned_and_dropped(tmp_path):
    (tmp_path / EVENT_FILE_NAME).write_bytes(
        (json.dumps(_event_document()) + "\n").encode() + b'{"seq":2'
    )

    with pytest.warns(EventLogWarning, match="truncated final event"):
        events = EventLogReader(tmp_path).load_events()

    assert [event.seq for event in events] == [1]


def test_complete_invalid_json_is_not_silently_skipped(tmp_path):
    (tmp_path / EVENT_FILE_NAME).write_text("not-json\n", encoding="utf-8")

    with pytest.raises(CassetteError, match="invalid JSONL record"):
        EventLogReader(tmp_path).load_events()


@pytest.mark.parametrize(
    ("second", "message"),
    [
        ({"seq": 1, "lamport": 2}, "seq values must be strictly increasing"),
        ({"seq": 2, "lamport": 1}, "lamport values must be strictly increasing"),
    ],
)
def test_native_event_order_must_be_strictly_monotonic(tmp_path, second, message):
    documents = [_event_document(), _event_document(**second)]
    (tmp_path / EVENT_FILE_NAME).write_text(
        "".join(json.dumps(document) + "\n" for document in documents),
        encoding="utf-8",
    )

    with pytest.raises(CassetteError, match=message):
        EventLogReader(tmp_path).load_events()
