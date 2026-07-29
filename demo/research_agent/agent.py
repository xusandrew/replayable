"""Small unmodified Anthropic research agent used by the Replayable MVP demo."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import anthropic
import httpx

WORKSPACE = Path("/workspace")
SYSTEM_PROMPT_PATH = WORKSPACE / "system_prompt.txt"
MODEL = "claude-haiku-4-5"
MAX_MODEL_CALLS = 8
MIN_MODEL_CALLS = 5

SYSTEM_PROMPT = """
You are a concise research agent. Research the user's topic and prepare a
fact-based report. You must use both available tools at least once before your
final answer. Use Hacker News search for technical/community evidence and the
weather tool for a concrete live-data example. Execute tools one at a time.
After gathering evidence, synthesize a markdown report with a short summary,
findings, and source notes. Do not invent facts that are absent from tool
results.
""".strip()

TOOLS = [
    {
        "name": "search_hacker_news",
        "description": "Search Hacker News stories through the Algolia API.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_waterloo_weather",
        "description": (
            "Get the current Open-Meteo forecast for Waterloo, Ontario. "
            "Use it as a live-data example relevant to the report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def search_hacker_news(arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    query = str(arguments.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    response = httpx.get(
        "https://hn.algolia.com/api/v1/search",
        params={"query": query, "tags": "story", "hitsPerPage": 5},
        timeout=30,
    )
    response.raise_for_status()
    document = response.json()
    hits = [
        {
            "title": hit.get("title"),
            "url": hit.get("url") or (
                f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            ),
            "points": hit.get("points"),
            "created_at": hit.get("created_at"),
        }
        for hit in document.get("hits", [])
    ]
    return {"query": query, "hits": hits}, [
        hit["url"] for hit in hits if isinstance(hit.get("url"), str)
    ]


def get_waterloo_weather(
    _arguments: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    url = "https://api.open-meteo.com/v1/forecast"
    response = httpx.get(
        url,
        params={
            "latitude": 43.4643,
            "longitude": -80.5204,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "timezone": "UTC",
        },
        timeout=30,
    )
    response.raise_for_status()
    return {
        "location": "Waterloo, Ontario",
        "forecast": response.json(),
    }, [str(response.url)]


TOOL_HANDLERS: dict[
    str,
    Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]],
] = {
    "search_hacker_news": search_hacker_news,
    "get_waterloo_weather": get_waterloo_weather,
}


def load_system_prompt() -> str:
    """Allow the negative demo to change behavior without rebuilding the image."""

    try:
        prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return SYSTEM_PROMPT
    if not prompt:
        raise ValueError(f"system prompt is empty at {SYSTEM_PROMPT_PATH}")
    return prompt


def _content_for_api(message: anthropic.types.Message) -> list[dict[str, Any]]:
    return [block.model_dump(exclude_none=True) for block in message.content]


def run(topic: str) -> None:
    client = anthropic.Anthropic()
    system_prompt = load_system_prompt()
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Research this topic: {topic}. Use both tools and produce the final "
                "report only after enough evidence has been gathered."
            ),
        }
    ]
    tools_used: set[str] = set()
    sources: list[str] = []
    text_sections: list[str] = []

    for call_number in range(1, MAX_MODEL_CALLS + 1):
        print(f"\n[model call {call_number}]", flush=True)
        with client.messages.stream(
            model=MODEL,
            max_tokens=700,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
            response = stream.get_final_message()
        print(flush=True)

        messages.append({"role": "assistant", "content": _content_for_api(response)})
        response_text = "\n".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if response_text:
            text_sections.append(response_text)

        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if tool_uses:
            tool_results: list[dict[str, Any]] = []
            for tool_use in tool_uses:
                print(f"[tool] {tool_use.name}", flush=True)
                try:
                    result, discovered_sources = TOOL_HANDLERS[tool_use.name](
                        dict(tool_use.input)
                    )
                    tools_used.add(tool_use.name)
                    sources.extend(discovered_sources)
                    content = json.dumps(result, sort_keys=True)
                    is_error = False
                except (KeyError, ValueError, httpx.HTTPError) as exc:
                    content = json.dumps({"error": str(exc)}, sort_keys=True)
                    is_error = True
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue

        missing = sorted(set(TOOL_HANDLERS) - tools_used)
        if call_number >= MIN_MODEL_CALLS and not missing:
            break
        instruction = "Continue the research with another concrete step."
        if missing:
            instruction += f" You still must call: {', '.join(missing)}."
        if call_number < MIN_MODEL_CALLS:
            instruction += (
                f" Make at least {MIN_MODEL_CALLS} model calls before finalizing."
            )
        messages.append({"role": "user", "content": instruction})
    else:
        raise RuntimeError("agent reached the model-call limit without a final report")

    if set(TOOL_HANDLERS) - tools_used:
        raise RuntimeError("agent did not use every required tool")
    report = text_sections[-1] if text_sections else f"# Research report: {topic}\n"
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "report.md").write_text(report.rstrip() + "\n", encoding="utf-8")
    (WORKSPACE / "sources.json").write_text(
        json.dumps(sorted(set(sources)), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[saved] {WORKSPACE / 'report.md'}", flush=True)


def main() -> None:
    topic = " ".join(sys.argv[1:]).strip()
    if not topic:
        raise SystemExit("usage: python /app/agent.py TOPIC")
    run(topic)


if __name__ == "__main__":
    main()
