import runpy
from pathlib import Path

_usage = runpy.run_path(
    Path(__file__).parents[1] / "scripts" / "benchmark.py"
)["_usage"]


def test_anthropic_sse_usage_parsing_handles_split_crlf_frames():
    flow = {
        "response": {
            "sse_chunks": [
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
        }
    }

    assert _usage(flow) == (123, 45, 0, 0)
