"""Milestone 3 request normalization, FIFO matching, and mismatch diffs."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode

from replayable.cassette import CassetteReader, sha256_bytes
from replayable.normalize_rules import (
    VOLATILE_SENTINEL,
    NormalizationRules,
)


@dataclass(frozen=True)
class RawRequest:
    method: str
    host: str
    port: int
    path: str
    query: str
    headers: Sequence[Sequence[str]]
    body: bytes
    scheme: str = ""
    body_sha256: str | None = None


@dataclass(frozen=True)
class NormalizedRequest:
    match_key: str
    pre_hash: str
    method: str
    host: str
    path: str
    query: str
    canonical_body: str
    diff_body: str

    def as_report_dict(self) -> dict[str, str]:
        return {
            "method": self.method,
            "host": self.host,
            "path": self.path,
            "query": self.query,
            "canonical_body": self.canonical_body,
            "match_key": self.match_key,
        }


@dataclass(frozen=True)
class RecordedEntry:
    sequence: int
    flow: dict[str, Any]
    normalized: NormalizedRequest


class ReplayMismatch(LookupError):
    """A live request did not match any remaining FIFO entry."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("live request did not match the cassette")
        self.payload = payload


def _content_type(headers: Sequence[Sequence[str]]) -> str:
    for pair in headers:
        if len(pair) == 2 and pair[0].lower() == "content-type":
            return pair[1].split(";", maxsplit=1)[0].strip().lower()
    return ""


def _normalized_host(request: RawRequest) -> str:
    host = request.host.lower()
    scheme = request.scheme.lower()
    is_default = (scheme == "https" and request.port == 443) or (
        scheme == "http" and request.port == 80
    )
    if not scheme and request.port in (80, 443):
        is_default = True
    return host if is_default else f"{host}:{request.port}"


def _normalize_json_value(
    value: Any,
    rules: NormalizationRules,
    *,
    key_name: str | None = None,
    preserve_current: bool = False,
) -> Any:
    field_names = rules.lowered_field_names
    preserved = rules.lowered_preserve

    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in field_names and lowered not in preserved:
                normalized[key_text] = VOLATILE_SENTINEL
            else:
                normalized[key_text] = _normalize_json_value(
                    child,
                    rules,
                    key_name=key_text,
                    preserve_current=lowered in preserved,
                )
        return normalized
    if isinstance(value, list):
        return [
            _normalize_json_value(
                child,
                rules,
                key_name=key_name,
                preserve_current=preserve_current,
            )
            for child in value
        ]
    if isinstance(value, str) and not preserve_current:
        if any(pattern.search(value) for pattern in rules.compiled_value_patterns):
            return VOLATILE_SENTINEL
        if (
            key_name is not None
            and re.search(r"time|date|ts", key_name, re.IGNORECASE)
            and re.fullmatch(r"(?:\d{10}|\d{13})", value)
        ):
            return VOLATILE_SENTINEL
    return value


def canonicalize_json(value: Any, rules: NormalizationRules) -> tuple[str, str]:
    """Return compact matching JSON and readable diff JSON."""

    normalized = _normalize_json_value(value, rules)
    compact = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    readable = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True)
    return compact, readable


def normalize_request(
    request: RawRequest,
    rules: NormalizationRules | None = None,
) -> NormalizedRequest:
    """Normalize a raw request and produce its inspectable pre-hash string."""

    rules = rules or NormalizationRules()
    method = request.method.upper()
    host = _normalized_host(request)
    path = request.path or "/"
    query_pairs = parse_qsl(request.query, keep_blank_values=True)
    query = urlencode(sorted(query_pairs, key=lambda pair: pair[0]))
    body_digest = request.body_sha256 or sha256_bytes(request.body)

    if _content_type(request.headers) == "application/json":
        try:
            parsed_body = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            canonical_body = body_digest
            diff_body = f"body_sha256: {body_digest}"
        else:
            canonical_body, diff_body = canonicalize_json(parsed_body, rules)
    else:
        canonical_body = body_digest
        diff_body = f"body_sha256: {body_digest}"

    pre_hash = "\n".join((method, host, path, query, canonical_body))
    match_key = hashlib.sha256(pre_hash.encode("utf-8")).hexdigest()
    return NormalizedRequest(
        match_key=match_key,
        pre_hash=pre_hash,
        method=method,
        host=host,
        path=path,
        query=query,
        canonical_body=canonical_body,
        diff_body=diff_body,
    )


def raw_request_from_record(
    flow: dict[str, Any],
    reader: CassetteReader,
) -> RawRequest:
    key = flow["key"]
    request = flow["request"]
    port = int(key["port"])
    scheme = "https" if port == 443 else "http" if port == 80 else ""
    return RawRequest(
        method=key["method"],
        host=key["host"],
        port=port,
        path=key["path"],
        query=request.get("query", ""),
        headers=request.get("headers", []),
        body=reader.read_body(request.get("body")),
        scheme=scheme,
        body_sha256=request.get("body_sha256"),
    )


def _unified_body_diff(
    live: NormalizedRequest,
    recorded: NormalizedRequest,
    sequence: int,
) -> str:
    return "".join(
        difflib.unified_diff(
            [f"{line}\n" for line in recorded.diff_body.splitlines()],
            [f"{line}\n" for line in live.diff_body.splitlines()],
            fromfile=f"recorded-flow-{sequence}",
            tofile="live-request",
        )
    )


# Candidate ranking truncates bodies and uses SequenceMatcher.quick_ratio so a
# mismatch report never stalls the proxy's request hook on large agent JSON.
RANKING_BODY_LIMIT = 4096


def _ranking_distance(first: str, second: str) -> float:
    """Return a lower-is-closer distance for deterministic candidate ranking."""

    return 1.0 - difflib.SequenceMatcher(a=first, b=second).quick_ratio()


class RequestMatcher:
    """Normalize live requests and pop recorded responses in FIFO order."""

    def __init__(
        self,
        entries: Iterable[RecordedEntry],
        rules: NormalizationRules,
    ) -> None:
        self.rules = rules
        self.entries = list(entries)
        self.entries_by_sequence = {entry.sequence: entry for entry in self.entries}
        self.queues: dict[str, deque[int]] = defaultdict(deque)
        for entry in self.entries:
            self.queues[entry.normalized.match_key].append(entry.sequence)

    @classmethod
    def from_flows(
        cls,
        flows: Iterable[dict[str, Any]],
        reader: CassetteReader,
        rules: NormalizationRules,
    ) -> RequestMatcher:
        entries = [
            RecordedEntry(
                sequence=int(flow["seq"]),
                flow=flow,
                normalized=normalize_request(
                    raw_request_from_record(flow, reader),
                    rules,
                ),
            )
            for flow in flows
        ]
        return cls(entries, rules)

    def match(self, live_request: RawRequest) -> dict[str, Any]:
        normalized_live = normalize_request(live_request, self.rules)
        queue = self.queues.get(normalized_live.match_key)
        if queue:
            sequence = queue.popleft()
            return self.entries_by_sequence[sequence].flow
        raise ReplayMismatch(self._mismatch_payload(normalized_live))

    def unconsumed_sequences(self) -> list[int]:
        return sorted(sequence for queue in self.queues.values() for sequence in queue)

    def _mismatch_payload(self, live: NormalizedRequest) -> dict[str, Any]:
        remaining = {
            sequence for queue in self.queues.values() for sequence in queue
        }
        remaining_entries = [
            entry for entry in self.entries if entry.sequence in remaining
        ]
        # Prefer unconsumed flows so diffs point at what could still match.
        # Fall back to the full cassette only when every flow was already used.
        pool = remaining_entries or self.entries
        same_route = [
            entry
            for entry in pool
            if (
                entry.normalized.method,
                entry.normalized.host,
                entry.normalized.path,
            )
            == (live.method, live.host, live.path)
        ]
        candidates = same_route or pool
        live_ranking_body = live.diff_body[:RANKING_BODY_LIMIT]
        scored = []
        for entry in candidates:
            distance = _ranking_distance(
                live_ranking_body,
                entry.normalized.diff_body[:RANKING_BODY_LIMIT],
            )
            scored.append((distance, entry.sequence, entry))
        scored.sort(key=lambda item: (item[0], item[1]))
        nearest = [
            {
                "seq": entry.sequence,
                "method": entry.normalized.method,
                "host": entry.normalized.host,
                "path": entry.normalized.path,
                "diff_size": round(distance, 6),
            }
            for distance, _sequence, entry in scored[:3]
        ]
        best_diff = (
            _unified_body_diff(live, scored[0][2].normalized, scored[0][1])
            if scored
            else ""
        )
        return {
            "live_request": live.as_report_dict(),
            "nearest_candidates": nearest,
            "diff": best_diff,
        }
