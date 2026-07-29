# Research-agent demo

This is the Milestone 5 workload. It imports Anthropic and HTTPX, but nothing
from Replayable. Across 5–8 streamed model calls it uses the keyless Hacker
News Algolia and Open-Meteo APIs, then writes `report.md` and `sources.json` to
`/workspace`.

From the repository root:

```sh
docker build -t replayable/agent-base:local images/agent-base
docker build -t replayable/research-agent:local demo/research_agent
cp demo/research_agent/.env.example demo/research_agent/.env
# Replace the placeholder in .env with a real ANTHROPIC_API_KEY.

uv run replayable record \
  --image replayable/research-agent:local \
  --env-file demo/research_agent/.env \
  --out cassettes/research-agent \
  -- python /app/agent.py "deterministic replay for LLM agents"

uv run replayable replay \
  --cassette cassettes/research-agent \
  --strict
```

Only the record command needs network access or a real key. Replay injects a
dummy value for `ANTHROPIC_API_KEY`, pins the recorded image and clock, and
serves the model/tool responses from the cassette.

## Negative prompt-change demo

Create a fresh workspace containing the supplied prompt with only
`concise` changed to `verbose`, then replay:

```sh
rm -rf /tmp/replayable-negative
mkdir -p /tmp/replayable-negative
cp demo/research_agent/system-prompt-changed.txt \
  /tmp/replayable-negative/system_prompt.txt

uv run replayable replay \
  --cassette cassettes/research-agent \
  --strict \
  --out-workspace /tmp/replayable-negative
```

The first Anthropic request receives HTTP 599, replay exits 2, and
`cassettes/research-agent/replay-report.json` shows the system-prompt change.
No upstream request is made.
