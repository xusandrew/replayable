"""Cassette inspection and match explanation for the `inspect` CLI command."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from replayable.cassette import CassetteError, CassetteReader, sse_chunk_bytes
from replayable.matcher import RawRequest, normalize_request
from replayable.normalize_rules import RulesError, discover_rules_path, load_rules
from replayable.runner import HarnessError


def _display_body(body: bytes) -> str | dict[str, str]:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(body).decode("ascii")}


def _inspect_cassette(cassette: Path, flow_sequence: int | None = None) -> str:
    """Render the manifest, flow table, or a selected flow with expanded bodies."""

    cassette = cassette.resolve()
    try:
        reader = CassetteReader(cassette)
        manifest = reader.load_manifest()
        loaded = reader.load_flows()
    except CassetteError as exc:
        raise HarnessError(f"cassette cannot be inspected: {exc}") from exc

    lines = ["Manifest", json.dumps(manifest, indent=2, sort_keys=True)]
    if loaded.dropped_truncated_final_line:
        lines.append("Warning: dropped a truncated final flows.jsonl record")

    if flow_sequence is not None:
        selected = next(
            (flow for flow in loaded.flows if flow.get("seq") == flow_sequence),
            None,
        )
        if selected is None:
            raise HarnessError(
                f"flow {flow_sequence} was not found; cassette has "
                f"{len(loaded.flows)} complete flows"
            )
        expanded = json.loads(json.dumps(selected))
        for side in ("request", "response"):
            body = expanded[side].get("body")
            if body is not None:
                expanded[side]["body"] = _display_body(reader.read_body(body))
        lines.extend([f"Flow {flow_sequence}", json.dumps(expanded, indent=2)])
        return "\n".join(lines)

    lines.extend(
        [
            "Flows",
            "SEQ  METHOD  ENDPOINT  STATUS  BODY_BYTES  SSE_CHUNKS",
        ]
    )
    for flow in loaded.flows:
        response = flow["response"]
        chunks = response.get("sse_chunks", [])
        if chunks:
            body = b"".join(sse_chunk_bytes(chunk) for chunk in chunks)
        else:
            body = reader.read_body(response.get("body"))
        key = flow["key"]
        query = flow["request"].get("query", "")
        endpoint = f"{key['host']}{key['path']}"
        if query:
            endpoint = f"{endpoint}?{query}"
        lines.append(
            f"{flow['seq']:>3}  {key['method']:<6}  {endpoint}  "
            f"{response['status']:>6}  {len(body):>10}  {len(chunks):>10}"
        )
    return "\n".join(lines)


def inspect_cassette(cassette: Path, flow_sequence: int | None = None) -> str:
    """Render a cassette with all malformed-bundle failures made actionable."""

    try:
        return _inspect_cassette(cassette, flow_sequence)
    except HarnessError:
        raise
    except (CassetteError, KeyError, TypeError, ValueError, OSError) as exc:
        raise HarnessError(f"cassette cannot be inspected: {exc}") from exc


def explain_match(request_path: Path, cassette: Path | None = None) -> str:
    """Explain normalization and hashing for a JSON-described request."""

    try:
        document = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("request document must be a JSON object")
        body_value = document.get("body", "")
        if isinstance(body_value, (dict, list)):
            body = json.dumps(body_value, separators=(",", ":")).encode()
        elif isinstance(body_value, str):
            body = body_value.encode()
        else:
            raise ValueError("body must be a string, object, or array")
        headers = document.get("headers", [])
        if isinstance(headers, dict):
            headers = list(headers.items())
        rules_path = discover_rules_path(
            cassette.resolve() if cassette is not None else None
        )
        rules = load_rules(rules_path)
        normalized = normalize_request(
            RawRequest(
                method=str(document["method"]),
                host=str(document["host"]),
                port=int(document.get("port", 443)),
                path=str(document.get("path", "/")),
                query=str(document.get("query", "")),
                headers=headers,
                body=body,
                scheme=str(document.get("scheme", "https")),
            ),
            rules,
        )
    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        RulesError,
    ) as exc:
        raise HarnessError(f"cannot explain request match: {exc}") from exc
    return json.dumps(
        {
            "ruleset_version": rules.version,
            "pre_hash": normalized.pre_hash,
            "match_key": normalized.match_key,
            "canonical_body": normalized.canonical_body,
        },
        indent=2,
        ensure_ascii=False,
    )
