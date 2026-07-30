from __future__ import annotations

import json

import pytest

from replayable.cassette import CassetteReader, CassetteWriter
from replayable.channels.providers.anthropic import (
    AnthropicParseError,
    ToolCall,
    ordered_tool_calls,
    parse_calls,
)


def _flow(seq, messages, response_events=()):
    payload = "".join(
        f"data: {json.dumps(event, separators=(',', ':'))}\n\n" for event in response_events
    )
    return {
        "seq": seq,
        "key": {
            "method": "POST",
            "host": "api.anthropic.com",
            "port": 443,
            "path": "/v1/messages",
        },
        "request": {
            "body": {"inline_utf8": json.dumps({"model": "claude-haiku-4-5", "messages": messages})}
        },
        "response": {
            "headers": [["content-type", "text/event-stream"]],
            "body": None,
            "sse_chunks": [{"data_utf8": payload}] if payload else [],
        },
    }


def _reader(tmp_path):
    writer = CassetteWriter(tmp_path)
    writer.root.mkdir(parents=True, exist_ok=True)
    writer.blob_directory.mkdir(exist_ok=True)
    return CassetteReader(tmp_path)


def test_response_tool_input_is_reassembled_across_sse_deltas(tmp_path):
    flow = _flow(
        1,
        [{"role": "user", "content": "search"}],
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "search",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"query":',
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '"replay"}',
                },
            },
            {"type": "content_block_stop", "index": 0},
        ],
    )

    calls = parse_calls(_reader(tmp_path), [flow])

    assert ordered_tool_calls(calls) == (ToolCall("tool-1", "search", {"query": "replay"}),)


def test_incomplete_response_tool_call_is_rejected(tmp_path):
    flow = _flow(
        1,
        [{"role": "user", "content": "search"}],
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "search",
                    "input": {},
                },
            }
        ],
    )

    with pytest.raises(AnthropicParseError, match="incomplete"):
        parse_calls(_reader(tmp_path), [flow])


def test_unreadable_sse_response_is_not_treated_as_no_tool_calls(tmp_path):
    flow = _flow(1, [{"role": "user", "content": "search"}])
    flow["response"]["sse_chunks"] = [{"data_utf8": "not an SSE document"}]

    with pytest.raises(AnthropicParseError, match="unreadable"):
        parse_calls(_reader(tmp_path), [flow])


def test_cumulative_request_history_is_deduplicated_by_tool_id(tmp_path):
    block = {
        "type": "tool_use",
        "id": "tool-1",
        "name": "search",
        "input": {"query": "replay"},
    }
    first = _flow(1, [{"role": "assistant", "content": [block]}])
    second = _flow(2, [{"role": "assistant", "content": [block]}])

    calls = parse_calls(_reader(tmp_path), [first, second])

    assert ordered_tool_calls(calls) == (ToolCall("tool-1", "search", {"query": "replay"}),)


def test_reused_tool_id_with_changed_input_is_rejected(tmp_path):
    first = _flow(
        1,
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "search",
                        "input": {"query": "one"},
                    }
                ],
            }
        ],
    )
    second = _flow(
        2,
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "search",
                        "input": {"query": "two"},
                    }
                ],
            }
        ],
    )

    with pytest.raises(AnthropicParseError, match="changed across requests"):
        ordered_tool_calls(parse_calls(_reader(tmp_path), [first, second]))


def test_malformed_tool_use_is_not_silently_omitted(tmp_path):
    flow = _flow(
        1,
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "search",
                        "input": "not-an-object",
                    }
                ],
            }
        ],
    )

    with pytest.raises(AnthropicParseError, match="malformed"):
        parse_calls(_reader(tmp_path), [flow])
