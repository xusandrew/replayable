# Running Replayable in CI

Two workflows.

| Workflow | Runs on | What it proves |
|---|---|---|
| `.github/workflows/ci.yml` | every push and PR | lint, unit tests, coverage gates, and Docker e2e against a self-recorded cassette |
| `.github/workflows/replay.yml` | every push and PR, **plus manual dispatch** | the determinism gate: a checked-in golden cassette replays byte-identically on a clean runner |

## Running it yourself

**Actions → Replay → Run workflow.** Pick a cassette and whether to run strict.

It needs **no API key and no secrets.** That is the point being demonstrated: a
recorded agent run reproduces offline, for $0.00, on a machine that has never
seen the agent's credentials.

From the CLI:

```bash
gh workflow run replay.yml -f cassette=research-agent -f strict=true
```

Then watch it:

```bash
gh run watch
```

## What a green run looks like

The job writes a verdict to the run summary:

| Check | Recorded | Replayed | Result |
| --- | --- | --- | --- |
| workspace sha256 | `72bd67c68b499f79…` | `72bd67c68b499f79…` | ✅ |
| stdout sha256 | `e8ddc3c0083eeb27…` | `e8ddc3c0083eeb27…` | ✅ |
| wall time | 32.04s | 0.60s | 53× faster |
| cost | real API spend | $0.00 | offline |

Every run uploads an artifact containing `last-replay.json`, the proxy log, the
replayed agent's stdout/stderr, and — if the replay diverged —
`replay-report.json` naming the first unmatched request.

## The two non-obvious things

### 1. The CA has to predate the cassette

Replay pins the container's clock to the moment the cassette was recorded. If
the CA mitmproxy signs with was created *after* that moment, the container sees
a certificate that is not yet valid and every TLS handshake fails.

mitmproxy backdates a generated CA by exactly **two days**. On a laptop that is
invisible, because your CA is older than your cassettes. In CI it is a dated
time bomb: a runner that generates a fresh CA can only replay cassettes recorded
in the last 48 hours, so the golden replay would begin failing on a fixed
calendar date, with a TLS error that says nothing about clocks.

So CI generates its CA with `scripts/make_replay_ca.py`, backdated ten years. No
private key is committed — the CA is created fresh per run and dies with the
job.

```bash
uv run python scripts/make_replay_ca.py --not-before-days 3650 --force
```

`replayable` itself detects the bad case and refuses to run rather than emitting
a confusing TLS failure, so if you ever see

> the mitmproxy CA … was generated after this cassette's recording time

this is what it means.

### 2. CI cannot assert image identity

The cassette pins the exact image ID it was recorded against. CI rebuilds the
image from `images/agent-base/Dockerfile`, so its ID differs **by
construction** — nothing is wrong, but the strict assertion cannot hold there.

| Where | Image identity | Workspace + stdout hashes |
|---|---|---|
| CI | rebuilt, `--allow-image-mismatch` | asserted exactly |
| A host with the recorded image | asserted exactly | asserted exactly |

Locally, with the recorded image present:

```bash
REPLAYABLE_RUN_E2E=1 REPLAYABLE_STRICT_IMAGE=1 uv run pytest tests/acceptance -m e2e
```

This is written down rather than left implicit because "green in CI" is
otherwise easy to read as a stronger claim than it is.

## Before anything else, run doctor

Both workflows run it, and it is the first thing to run locally too:

```bash
uv run replayable doctor
```

```
[ok  ] mitmdump      /path/to/.venv/bin/mitmdump
[ok  ] docker        server 28.4.0
[ok  ] mitmproxy CA  valid until 2036-07-09
[ok  ] proxy port    8080 is free
[ok  ] clock skew    0.4s between host and Docker daemon
[ok  ] host-gateway  host.docker.internal -> 192.168.65.254

All checks passed. Ready to record and replay.
```

Each check exists because its failure mode is misleading in practice:

| Check | What it catches | How it would otherwise appear |
|---|---|---|
| mitmdump | dependencies not installed | "mitmdump was not found" partway into a run |
| docker | daemon down, or older than 20.10 | container start failure, or `host.docker.internal` not resolving |
| mitmproxy CA | never generated, or expired | TLS handshake failures inside the container |
| proxy port | a crashed run still holds 8080 | the proxy never becomes ready, then a timeout |
| clock skew | Docker VM clock drifted after host sleep | certificate-validity errors that look like a CA problem |
| host-gateway | runner cannot route container → host | every agent request fails, looking like the agent's bug |

Exit codes follow the harness contract: `0` all clear, `3` something needs
fixing. Warnings do not fail. `--json` emits machine-readable output;
`--skip-container-checks` avoids pulling `alpine:3`.
