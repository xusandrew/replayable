"""Versioned event-log read model with transparent v1 flow derivation."""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from replayable.cassette import CassetteError, CassetteReader

EVENT_FILE_NAME = "events.jsonl"


class EventChannel(StrEnum):
    """Stable event-channel identifiers persisted in cassettes."""

    NETWORK = "network"
    MODEL = "model"
    TOOL = "tool"
    FILESYSTEM = "filesystem"
    PROCESS = "process"


class EventKind(StrEnum):
    """Stable event-kind identifiers persisted in cassettes."""

    HTTP_EXCHANGE = "http.exchange"
    MODEL_CALL = "model.call"
    TOOL_CALL = "tool.call"
    FILESYSTEM_SNAPSHOT = "filesystem.snapshot"
    PROCESS_EXIT = "process.exit"


KINDS_BY_CHANNEL = {
    EventChannel.NETWORK: frozenset({EventKind.HTTP_EXCHANGE}),
    EventChannel.MODEL: frozenset({EventKind.MODEL_CALL}),
    EventChannel.TOOL: frozenset({EventKind.TOOL_CALL}),
    EventChannel.FILESYSTEM: frozenset({EventKind.FILESYSTEM_SNAPSHOT}),
    EventChannel.PROCESS: frozenset({EventKind.PROCESS_EXIT}),
}


class EventLogWarning(UserWarning):
    """A forward-compatible event record was skipped."""


class _UnknownEventType(ValueError):
    """An event channel or kind is newer than this reader."""


@dataclass(frozen=True)
class Event:
    """One causally ordered observation in a cassette."""

    seq: int
    lamport: int
    t_rel: float
    channel: EventChannel
    kind: EventKind
    scope: str
    key: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "lamport": self.lamport,
            "t_rel": self.t_rel,
            "channel": str(self.channel),
            "kind": str(self.kind),
            "scope": self.scope,
            "key": self.key,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Event:
        """Validate a persisted event while distinguishing unknown event types."""

        channel_value = value.get("channel")
        kind_value = value.get("kind")
        try:
            channel = EventChannel(channel_value)
        except (TypeError, ValueError) as exc:
            raise _UnknownEventType(f"unknown event channel {channel_value!r}") from exc
        try:
            kind = EventKind(kind_value)
        except (TypeError, ValueError) as exc:
            raise _UnknownEventType(f"unknown event kind {kind_value!r}") from exc
        if kind not in KINDS_BY_CHANNEL[channel]:
            raise _UnknownEventType(
                f"event kind {kind.value!r} is not valid for channel {channel.value!r}"
            )

        seq = _required_int(value, "seq", minimum=1)
        lamport = _required_int(value, "lamport", minimum=1)
        t_rel_value = value.get("t_rel")
        if (
            isinstance(t_rel_value, bool)
            or not isinstance(t_rel_value, (int, float))
            or not math.isfinite(t_rel_value)
            or t_rel_value < 0
        ):
            raise CassetteError("event t_rel must be a finite non-negative number")
        scope = value.get("scope")
        key = value.get("key")
        payload = value.get("payload")
        if not isinstance(scope, str):
            raise CassetteError("event scope must be a string")
        if not isinstance(key, str):
            raise CassetteError("event key must be a string")
        if not isinstance(payload, dict):
            raise CassetteError("event payload must be an object")
        return cls(
            seq=seq,
            lamport=lamport,
            t_rel=float(t_rel_value),
            channel=channel,
            kind=kind,
            scope=scope,
            key=key,
            payload=payload,
        )


def _required_int(value: dict[str, Any], name: str, *, minimum: int) -> int:
    field = value.get(name)
    if isinstance(field, bool) or not isinstance(field, int) or field < minimum:
        raise CassetteError(f"event {name} must be an integer >= {minimum}")
    return field


def _event_key(flow: dict[str, Any]) -> tuple[str, str]:
    key = flow.get("key")
    if not isinstance(key, dict):
        raise CassetteError("flow key must be an object to derive events")
    method = key.get("method")
    host = key.get("host")
    port = key.get("port")
    path = key.get("path")
    if (
        not isinstance(method, str)
        or not isinstance(host, str)
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not isinstance(path, str)
    ):
        raise CassetteError("flow key is invalid and cannot be converted to an event")
    return host, f"{method} {host}:{port}{path}"


def event_from_flow(flow: dict[str, Any], *, lamport: int) -> Event:
    """Derive one network exchange event without mutating the source flow."""

    seq = _required_int(flow, "seq", minimum=1)
    timing = flow.get("timing")
    if not isinstance(timing, dict):
        raise CassetteError(f"flow {seq} timing must be an object")
    started = timing.get("started")
    completed = timing.get("completed")
    if (
        isinstance(started, bool)
        or not isinstance(started, (int, float))
        or not math.isfinite(started)
        or started < 0
    ):
        raise CassetteError(f"flow {seq} timing.started must be non-negative")
    if (
        isinstance(completed, bool)
        or not isinstance(completed, (int, float))
        or not math.isfinite(completed)
        or completed < started
    ):
        raise CassetteError(f"flow {seq} timing.completed must be >= timing.started")
    scope, key = _event_key(flow)
    return Event(
        seq=seq,
        lamport=lamport,
        t_rel=float(started),
        channel=EventChannel.NETWORK,
        kind=EventKind.HTTP_EXCHANGE,
        scope=scope,
        key=key,
        payload={
            "duration_seconds": float(completed - started),
            "flow": flow,
        },
    )


def derive_events_from_flows(flows: Iterable[dict[str, Any]]) -> list[Event]:
    """Expose a v1 flow stream through the v2 event model, entirely in memory."""

    events = [event_from_flow(flow, lamport=index) for index, flow in enumerate(flows, start=1)]
    _validate_event_order(events)
    return events


def _validate_event_order(events: Iterable[Event]) -> None:
    previous_seq = 0
    previous_lamport = 0
    for event in events:
        if event.seq <= previous_seq:
            raise CassetteError("event seq values must be strictly increasing")
        if event.lamport <= previous_lamport:
            raise CassetteError("event lamport values must be strictly increasing")
        previous_seq = event.seq
        previous_lamport = event.lamport


class EventLogReader:
    """Read native events when present, otherwise derive them from v1 flows."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.event_path = root / EVENT_FILE_NAME

    def load_events(self) -> list[Event]:
        if not self.event_path.exists():
            loaded = CassetteReader(self.root).load_flows()
            if loaded.dropped_truncated_final_line:
                warnings.warn(
                    f"derived events after dropping the truncated final flow in {self.root}",
                    EventLogWarning,
                    stacklevel=2,
                )
            return derive_events_from_flows(loaded.flows)
        try:
            raw = self.event_path.read_bytes()
        except OSError as exc:
            raise CassetteError(f"event file is unreadable at {self.event_path}: {exc}") from exc

        events: list[Event] = []
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                is_unterminated_final_line = index == len(lines) - 1 and not raw.endswith(
                    (b"\n", b"\r")
                )
                if is_unterminated_final_line:
                    warnings.warn(
                        f"dropped truncated final event at {self.event_path}:{index + 1}",
                        EventLogWarning,
                        stacklevel=2,
                    )
                    break
                raise CassetteError(
                    f"invalid JSONL record at {self.event_path}:{index + 1}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise CassetteError(f"event at {self.event_path}:{index + 1} must be an object")
            try:
                event = Event.from_dict(value)
            except _UnknownEventType as exc:
                warnings.warn(
                    f"skipped {exc} at {self.event_path}:{index + 1}",
                    EventLogWarning,
                    stacklevel=2,
                )
                continue
            events.append(event)
        _validate_event_order(events)
        return events
