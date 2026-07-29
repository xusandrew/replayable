# Acceptance tests

These tests exist to answer one question, repeatedly, for the rest of the V1 build:

> Does a recorded agent run still replay byte-identically?

Everything after this PR — extracting `runner.py` into `core/`, the cassette v2
event log, the policy engine, the verdict engine, the fork replay engine — is a
change to the machinery that produces those bytes. Each is only safe if this
directory stays green.

## The corpus

Checked in under `tests/fixtures/cassettes/`, reached via `fixtures.corpus`:

| Cassette | What it is | Size |
|---|---|---|
| `research-agent` | The M5 demo: 20 flows of `demo/research_agent` against `claude-haiku-4-5`, with two keyless tools (HN Algolia, Open-Meteo). Writes `report.md` + `sources.json`. | 392K |
| `curl-demo` | The M1 workload: 4 plain HTTP flows, no LLM. A fast smoke fixture. | 16K |

Only *recording* artifacts are checked in. Replay outputs (`last-replay.json`,
`replay-*.log`, `replay-state.json`) are products of running the tests, so tests
that replay do so against a copy in `tmp_path` — see `copy_fixture_cassette`.

The bundles contain no secrets. The recorded `x-api-key` header is `[REDACTED]`
on disk and the manifest stores only the *name* `ANTHROPIC_API_KEY`, which is
what lets replay run with dummy credentials. `test_golden_cassette_carries_no_unredacted_secret`
pins that.

## The three tiers

Ordered by what they need, so the bar means the same thing wherever it runs.

### Tier 1 — offline structural checks (always run)

No Docker, no network, no credentials. Proves the cassette and the matcher agree:
every recorded request re-normalizes to its own match key and pops in FIFO
order, leaving nothing unconsumed. If a normalization-ruleset change breaks
replay, this fails in milliseconds instead of minutes.

```bash
uv run pytest tests/acceptance
```

### Tier 2 — replay determinism (`REPLAYABLE_RUN_E2E=1`)

The real acceptance criterion. Replays the cassette in Docker and asserts both
golden hashes. **Needs no API key** — that is the entire point.

```bash
REPLAYABLE_RUN_E2E=1 uv run pytest tests/acceptance -m e2e
```

Requires Docker, the mitmproxy CA (`replayable doctor` will tell you), and the
recorded image available locally or a rebuild of it.

### Tier 3 — strict image identity (`REPLAYABLE_STRICT_IMAGE=1`)

Opt-in, and deliberately so.

The cassette pins the exact image ID it was recorded against
(`sha256:cd398ef5…`). On a machine holding that image, replay can assert it —
which removes the last degree of freedom from the determinism claim.

**In GitHub Actions the image is rebuilt from `images/agent-base/Dockerfile`, so
its ID differs by construction.** CI therefore runs tiers 1 and 2 and asserts the
workspace and stdout hashes only. That is a slightly weaker claim than the local
run, and it is called out here rather than left for a reader to discover:

| Where | Image identity | Workspace + stdout hashes |
|---|---|---|
| This repo's CI | rebuilt, `--allow-image-mismatch` | asserted |
| A host with the recorded image | asserted exactly | asserted |

To preserve the recorded image against a future rebuild, it is tagged
`replayable/research-agent:golden` locally. Re-recording the cassette is the
only supported way to move the golden hashes, and it must happen in the same
commit that updates the constants in `test_m5_golden.py`.

## When these fail

A failure here is never "flaky, re-run it". It means one of:

1. **A refactor changed replay behaviour.** Bisect against the last green PR.
2. **The normalization ruleset changed.** Tier 1 fails first and names the flow.
3. **The cassette was re-recorded** without updating the golden constants.
4. **The environment is wrong** — missing CA, no Docker, expired certificate.
   Run `replayable doctor` (added in PR 3).
