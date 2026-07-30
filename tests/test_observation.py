from __future__ import annotations

from pathlib import Path

from fixtures.corpus import fixture_cassette

from replayable.channels.providers.anthropic import ToolCall
from replayable.verdict.observation import (
    ModelSummary,
    Observation,
    Transcript,
    build_observation,
    serialize_observation,
)
from replayable.verdict.usage import TokenUsage


def test_research_fixture_has_exact_tool_sequence_and_aggregate_metrics():
    observation = build_observation(fixture_cassette("research-agent"))

    assert [(call.name, call.input) for call in observation.tool_calls] == [
        ("search_hacker_news", {"query": "deterministic replay LLM agents"}),
        (
            "search_hacker_news",
            {"query": "LLM agent reproducibility deterministic"},
        ),
        ("get_waterloo_weather", {}),
        (
            "search_hacker_news",
            {"query": "agent debugging replay execution trace"},
        ),
        (
            "search_hacker_news",
            {"query": "LLM agent testing framework validation"},
        ),
        (
            "search_hacker_news",
            {"query": "agent execution trace logging reproducibility"},
        ),
        (
            "search_hacker_news",
            {"query": "AI agent debugging tools observability"},
        ),
        (
            "search_hacker_news",
            {"query": "LLM agent deterministic behavior verification"},
        ),
        (
            "search_hacker_news",
            {"query": "agent prompt caching state management"},
        ),
        (
            "search_hacker_news",
            {"query": "LLM non-determinism output consistency"},
        ),
        (
            "search_hacker_news",
            {"query": "agent state persistence checkpoint"},
        ),
        (
            "search_hacker_news",
            {"query": "LLM reproducibility seeded generation"},
        ),
        ("get_waterloo_weather", {}),
    ]
    assert observation.model.calls == 7
    assert observation.model.models == ("claude-haiku-4-5",)
    assert observation.model.tokens == TokenUsage(input=24_613, output=2_169)
    assert observation.model.estimated_cost_usd == 0.035458
    assert observation.model.usage_complete
    assert observation.model.cost_complete
    assert observation.exit_code == 0
    assert observation.wall_time_seconds == 32.04012729198439
    assert observation.workspace_sha256 == (
        "72bd67c68b499f79ee72551e98acc72f2c01cedc94bf7a02d9ba99e2d561295d"
    )
    assert [item["path"] for item in observation.workspace_files] == [
        "report.md",
        "sources.json",
    ]


def test_observation_serialization_matches_golden_file():
    observation = Observation(
        transcript=Transcript(
            stdout="done\n",
            stderr="",
            stdout_sha256="d" * 64,
            stderr_sha256="e" * 64,
        ),
        tool_calls=(ToolCall("tool-1", "search", {"query": "replay"}),),
        workspace_sha256=("72bd67c68b499f79ee72551e98acc72f2c01cedc94bf7a02d9ba99e2d561295d"),
        workspace_files=(
            {
                "path": "report.md",
                "size": 4,
                "sha256": "f" * 64,
                "type": "file",
                "mode": "0644",
            },
        ),
        exit_code=0,
        model=ModelSummary(
            calls=1,
            models=("claude-haiku-4-5",),
            usage_complete=True,
            tokens=TokenUsage(input=10, output=20, cache_write=2, cache_read=3),
            cost_complete=True,
            estimated_cost_usd=0.00012,
        ),
        wall_time_seconds=1.25,
    )
    golden = (Path(__file__).parent / "fixtures" / "observations" / "minimal.json").read_text(
        encoding="utf-8"
    )

    assert serialize_observation(observation) == golden
