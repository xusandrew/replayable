from __future__ import annotations

import base64
import gzip
import json

import pytest
from fixtures.corpus import fixture_cassette

from replayable.cassette import CassetteReader
from replayable.verdict.usage import TokenUsage, estimate_cost_usd, extract_usage


def _flow(chunks, *, content_encoding=""):
    headers = [["content-encoding", content_encoding]] if content_encoding else []
    return {
        "response": {
            "headers": headers,
            "sse_chunks": chunks,
        }
    }


def test_usage_parser_handles_split_crlf_frames():
    flow = _flow(
        [
            {
                "data_utf8": (
                    'event: message_start\r\ndata: {"type":"message_start",'
                    '"message":{"usage":{"input_tokens":123,"output_tokens":1}}}\r'
                )
            },
            {
                "data_utf8": (
                    '\n\r\nevent: message_delta\r\ndata: {"type":"message_delta",'
                    '"usage":{"output_tokens":45}}\r\n\r\n'
                )
            },
        ]
    )

    assert extract_usage(flow) == TokenUsage(input=123, output=45)


def test_usage_parser_decompresses_gzip_across_transport_chunks():
    payload = (
        b'event: message_start\ndata: {"message":{"usage":{"input_tokens":200,'
        b'"cache_creation_input_tokens":20,"cache_read_input_tokens":10}}}\n\n'
        b'event: message_delta\ndata: {"usage":{"output_tokens":30}}\n\n'
    )
    compressed = gzip.compress(payload)
    split = len(compressed) // 2
    chunks = [
        {
            "data_base64": base64.b64encode(chunk).decode("ascii"),
        }
        for chunk in (compressed[:split], compressed[split:])
    ]

    assert extract_usage(_flow(chunks, content_encoding="gzip")) == TokenUsage(
        input=200,
        output=30,
        cache_write=20,
        cache_read=10,
    )


def test_unreadable_or_unsupported_encoding_has_no_fabricated_usage():
    corrupt = [{"data_base64": base64.b64encode(b"not gzip").decode("ascii")}]
    plain = [{"data_utf8": 'data: {"usage":{"input_tokens":10}}\n\n'}]

    assert extract_usage(_flow(corrupt, content_encoding="gzip")) is None
    assert extract_usage(_flow(plain, content_encoding="br")) is None


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": "10", "output_tokens": 2},
        {"input_tokens": 10, "output_tokens": -1},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": 10},
    ],
)
def test_malformed_or_incomplete_counters_have_no_fabricated_usage(usage):
    flow = _flow(
        [{"data_utf8": f"data: {json.dumps({'usage': usage})}\n\n"}]
    )

    assert extract_usage(flow) is None


def test_non_streaming_json_usage_is_supported():
    flow = {
        "response": {
            "headers": [["content-type", "application/json"]],
            "body": {
                "inline_utf8": (
                    '{"usage":{"input_tokens":40,"output_tokens":12,"cache_read_input_tokens":5}}'
                )
            },
        }
    }

    assert extract_usage(flow) == TokenUsage(input=40, output=12, cache_read=5)


def test_cost_accounting_includes_cache_multipliers_and_unknown_model_is_none():
    usage = TokenUsage(input=100, output=20, cache_write=40, cache_read=50)

    assert estimate_cost_usd("claude-haiku-4-5", usage) == 0.000255
    assert estimate_cost_usd("unknown-model", usage) is None


def test_golden_gzip_streams_have_exact_model_call_usage():
    reader = CassetteReader(fixture_cassette("research-agent"))
    usages = [
        usage
        for flow in reader.load_flows().flows
        if flow["key"]["host"] == "api.anthropic.com"
        if (usage := extract_usage(flow)) is not None
    ]

    assert len(usages) == 7
    assert sum(usage.input for usage in usages) == 24_613
    assert sum(usage.output for usage in usages) == 2_169
    total_cost = sum(estimate_cost_usd("claude-haiku-4-5", usage) or 0.0 for usage in usages)
    assert total_cost == 0.035458
