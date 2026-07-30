"""Anthropic Messages API observation adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from replayable.cassette import CassetteError, CassetteReader
from replayable.verdict.usage import (
    TokenUsage,
    decode_response_body,
    estimate_cost_usd,
    extract_usage,
    sse_documents,
)

ANTHROPIC_HOST = "api.anthropic.com"
MESSAGES_PATH = "/v1/messages"


class AnthropicParseError(ValueError):
    """A recognized Anthropic model call could not be observed safely."""


@dataclass(frozen=True)
class ToolCall:
    """One provider-neutral tool invocation in model execution order."""

    id: str
    name: str
    input: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "input": self.input}


@dataclass(frozen=True)
class ModelCall:
    """The stable facts extracted from one Anthropic Messages request."""

    seq: int
    model: str
    tool_calls: tuple[ToolCall, ...]
    usage: TokenUsage | None
    estimated_cost_usd: float | None


def is_model_call(flow: dict[str, Any]) -> bool:
    key = flow.get("key")
    return (
        isinstance(key, dict)
        and key.get("host") == ANTHROPIC_HOST
        and key.get("path") == MESSAGES_PATH
        and key.get("method") == "POST"
    )


def _request_document(
    reader: CassetteReader,
    flow: dict[str, Any],
) -> dict[str, Any]:
    request = flow.get("request")
    if not isinstance(request, dict):
        raise AnthropicParseError("Anthropic flow request must be an object")
    representation = request.get("body")
    if representation is not None and not isinstance(representation, dict):
        raise AnthropicParseError("Anthropic request body representation is invalid")
    try:
        raw = reader.read_body(representation)
        document = json.loads(raw)
    except (CassetteError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AnthropicParseError("Anthropic request body is not valid JSON") from exc
    if not isinstance(document, dict):
        raise AnthropicParseError("Anthropic request body must be a JSON object")
    return document


def _content_tool_calls(
    content: object,
    *,
    location: str,
) -> tuple[ToolCall, ...]:
    if not isinstance(content, list):
        return ()
    calls: list[ToolCall] = []
    for block_index, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_id = block.get("id")
        name = block.get("name")
        tool_input = block.get("input")
        if (
            not isinstance(tool_id, str)
            or not tool_id
            or not isinstance(name, str)
            or not name
            or not isinstance(tool_input, dict)
        ):
            raise AnthropicParseError(
                f"Anthropic tool_use block {location}:{block_index} is malformed"
            )
        calls.append(ToolCall(tool_id, name, tool_input))
    return tuple(calls)


def _request_tool_calls(document: dict[str, Any]) -> tuple[ToolCall, ...]:
    messages = document.get("messages")
    if not isinstance(messages, list):
        raise AnthropicParseError("Anthropic request messages must be an array")
    calls: list[ToolCall] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise AnthropicParseError(
                f"Anthropic request message {message_index} must be an object"
            )
        calls.extend(
            _content_tool_calls(
                message.get("content"),
                location=f"request message {message_index}",
            )
        )
    return tuple(calls)


def _sse_response_tool_calls(flow: dict[str, Any]) -> tuple[ToolCall, ...]:
    pending: dict[int, dict[str, Any]] = {}
    completed: set[int] = set()
    documents = sse_documents(flow)
    response = flow.get("response")
    if (
        isinstance(response, dict)
        and isinstance(response.get("sse_chunks"), list)
        and response["sse_chunks"]
        and not documents
    ):
        raise AnthropicParseError("Anthropic SSE response is unreadable")
    for document in documents:
        event_type = document.get("type")
        index = document.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        if event_type == "content_block_start":
            block = document.get("content_block")
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            name = block.get("name")
            initial_input = block.get("input")
            if (
                not isinstance(tool_id, str)
                or not tool_id
                or not isinstance(name, str)
                or not name
                or not isinstance(initial_input, dict)
            ):
                raise AnthropicParseError(
                    f"Anthropic response tool_use block {index} is malformed"
                )
            if index in pending:
                raise AnthropicParseError(
                    f"Anthropic response content block {index} started twice"
                )
            pending[index] = {
                "id": tool_id,
                "name": name,
                "input": initial_input,
                "partial_json": "",
            }
        elif event_type == "content_block_delta" and index in pending:
            delta = document.get("delta")
            if not isinstance(delta, dict) or delta.get("type") != "input_json_delta":
                continue
            partial = delta.get("partial_json")
            if not isinstance(partial, str):
                raise AnthropicParseError(
                    f"Anthropic response tool input delta {index} is malformed"
                )
            pending[index]["partial_json"] += partial
        elif event_type == "content_block_stop" and index in pending:
            completed.add(index)

    calls: list[ToolCall] = []
    for index, pending_call in sorted(pending.items()):
        if index not in completed:
            raise AnthropicParseError(
                f"Anthropic response tool_use block {index} is incomplete"
            )
        partial_json = pending_call["partial_json"]
        tool_input = pending_call["input"]
        if partial_json:
            if tool_input:
                raise AnthropicParseError(
                    f"Anthropic response tool input {index} is ambiguous"
                )
            try:
                tool_input = json.loads(partial_json)
            except json.JSONDecodeError as exc:
                raise AnthropicParseError(
                    f"Anthropic response tool input {index} is invalid JSON"
                ) from exc
            if not isinstance(tool_input, dict):
                raise AnthropicParseError(
                    f"Anthropic response tool input {index} must be an object"
                )
        calls.append(ToolCall(pending_call["id"], pending_call["name"], tool_input))
    return tuple(calls)


def _nonstream_response_tool_calls(
    reader: CassetteReader,
    flow: dict[str, Any],
) -> tuple[ToolCall, ...]:
    response = flow.get("response")
    if not isinstance(response, dict):
        return ()
    representation = response.get("body")
    if representation is None:
        return ()
    if not isinstance(representation, dict):
        raise AnthropicParseError("Anthropic response body representation is invalid")
    try:
        raw = decode_response_body(flow, reader.read_body(representation))
        document = json.loads(raw)
    except (CassetteError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        status = response.get("status")
        if isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300:
            raise AnthropicParseError(
                "successful Anthropic response body is not valid JSON"
            ) from exc
        return ()
    if not isinstance(document, dict):
        return ()
    return _content_tool_calls(document.get("content"), location="response")


def _response_tool_calls(
    reader: CassetteReader,
    flow: dict[str, Any],
) -> tuple[ToolCall, ...]:
    sse_calls = _sse_response_tool_calls(flow)
    return sse_calls if sse_calls else _nonstream_response_tool_calls(reader, flow)


def parse_model_call(
    reader: CassetteReader,
    flow: dict[str, Any],
) -> ModelCall:
    """Parse a recognized Messages API flow without leaking provider schema."""

    if not is_model_call(flow):
        raise AnthropicParseError("flow is not an Anthropic Messages API call")
    seq = flow.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise AnthropicParseError("Anthropic flow seq must be a positive integer")
    document = _request_document(reader, flow)
    model = document.get("model")
    if not isinstance(model, str) or not model:
        raise AnthropicParseError("Anthropic request model must be a non-empty string")
    usage = extract_usage(flow)
    cost = estimate_cost_usd(model, usage) if usage is not None else None
    return ModelCall(
        seq=seq,
        model=model,
        tool_calls=_request_tool_calls(document) + _response_tool_calls(reader, flow),
        usage=usage,
        estimated_cost_usd=cost,
    )


def parse_calls(
    reader: CassetteReader,
    flows: list[dict[str, Any]],
) -> tuple[ModelCall, ...]:
    """Parse all Anthropic calls in flow order, rejecting duplicate sequences."""

    calls = tuple(
        parse_model_call(reader, flow) for flow in flows if is_model_call(flow)
    )
    sequences = [call.seq for call in calls]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise AnthropicParseError(
            "Anthropic model-call sequences must be unique and ordered"
        )
    return calls


def ordered_tool_calls(calls: tuple[ModelCall, ...]) -> tuple[ToolCall, ...]:
    """Collapse cumulative message histories while preserving first invocation order."""

    ordered: list[ToolCall] = []
    seen: dict[str, ToolCall] = {}
    for call in calls:
        for tool_call in call.tool_calls:
            previous = seen.get(tool_call.id)
            if previous is None:
                seen[tool_call.id] = tool_call
                ordered.append(tool_call)
            elif previous != tool_call:
                raise AnthropicParseError(
                    f"Anthropic tool call {tool_call.id!r} changed across requests"
                )
    return tuple(ordered)
