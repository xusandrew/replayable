# Design notes

How Replayable actually works, as built. For the decisions behind it see the
ADRs; for what it cannot do see [`../limitations.md`](../limitations.md).

## The premise

An agent is nondeterministic only because of a small number of **channels**
through which fresh randomness enters a run. Intercept those channels and a run
becomes reproducible. The MVP covers three:

| Channel | How it is frozen | Where |
|---|---|---|
| Model call + tools | TLS-intercepting proxy at the container boundary records every request/response, then serves them back | `addons/`, `matcher.py` |
| Clock | `libfaketime` via `LD_PRELOAD`, pinned to the recorded `t0` in **both** record and replay | `runner.replay_time_environment` |
| Filesystem | Fresh container from a pinned image, deterministic tar + SHA-256 of `/workspace` at exit | `snapshot.py` |

Randomness is deliberately *not* pinned. The matcher absorbs it instead by
normalizing volatile fields out of the match key — see ADR-4. `PYTHONHASHSEED=0`
is set in both modes so dict iteration order cannot leak in.

## The shape of a run

```
CLI ──► mitmdump subprocess (host, loopback or docker bridge)
 │            ▲
 │            │ HTTP_PROXY / HTTPS_PROXY, CA mounted at /etc/replayable/ca.pem
 ▼            │
docker run ── agent (unmodified, zero Replayable imports)
 │
 ▼
cassette bundle/
```

Record and replay differ only in which addon `mitmdump` loads. Everything else —
the container contract, the clock pinning, the workspace snapshot — is identical,
which is what makes the two runs comparable at all.

## Why the proxy sits at the container boundary

The alternative is monkeypatching the model SDK, which is what `vcr.py`-style
tools do. That requires instrumenting the agent, and it only covers the one
library you patched. A proxy at the boundary covers **every** HTTP client in the
container — the model SDK, an arbitrary tool's `requests` call, `curl` in a shell
script — without the agent knowing Replayable exists. That transparency is the
differentiating claim, so it is a structural choice, not a convenience.

The cost is that non-HTTP and cert-pinned traffic is invisible. Both are
documented limitations with runnable reproductions.

## The matcher is the intellectual core

Replay has to decide *which* recorded response answers a live request. Byte
equality fails immediately: agent requests embed UUIDs, timestamps and prior
tool-call IDs that differ every run.

So matching is **normalize-then-exact**:

1. Method, host (port-normalized), path, query (sorted).
2. JSON bodies canonicalized — keys sorted, floats stably rendered.
3. Volatile fields replaced with a sentinel, by field name (`request_id`,
   `tool_use_id`, …) and by value pattern (UUIDv4, ISO-8601).
4. SHA-256 of the joined result is the match key.

Requests with the same key are served **first-in-first-out**, so a repeated
identical call gets its own recorded response in order. Headers are excluded
entirely — they carry auth and tracing noise and never distinguish a request.

Two rules keep this honest:

- **Never fall back to a nearest match.** An unmatched request is HTTP 599 plus a
  diff, never a plausible-looking wrong answer. A test that silently passes on
  the wrong response is worse than no test.
- **Store raw, normalize at load.** Cassettes keep the redacted-but-unnormalized
  request, and rules are applied when the cassette is read. A recording is
  therefore never frozen to the ruleset that happened to be current when it was
  made — but the manifest pins `ruleset_version`, so a changed ruleset is an
  explicit error rather than a silent behaviour change.

## Offline by construction

The replay addon sets `flow.response` in mitmproxy's **request** hook, before any
upstream connection is attempted. This is a structural guarantee, not a policy:
there is no code path from replay to the network. Pulling the network cable
changes nothing about a replay, which is exactly the property CI needs.

## Secrets

Redaction happens at write time, before hashing, so the hash covers the redacted
form and a cassette can never contain a live credential. Secrets reach the proxy
addon through a `0600` temp file rather than the environment. Replay substitutes
`[REDACTED:NAME]` placeholders, which is why **replay needs no real
credentials** — the property that makes cassettes shareable and CI free.

## Module map

| Module | Responsibility |
|---|---|
| `cli.py` | Typer entry points |
| `runner.py` | Run orchestration: proxy lifecycle, container invocation, verification |
| `cassette.py` | Bundle read/write, manifest, content-addressed blobs |
| `matcher.py` | Normalization pipeline, FIFO matching, mismatch diffs |
| `normalize_rules.py` | Default ruleset and `replayable.toml` overrides |
| `redact.py` | Write-time secret detection and redaction |
| `snapshot.py` | Deterministic workspace archive and file-level diff |
| `inspection.py` | `inspect` and `--explain-match` rendering |
| `addons/` | The two mitmproxy addons (record, replay) |

## Exit codes

Distinguishing "the agent failed" from "replay diverged" is load-bearing for CI:
one is the agent's problem, the other is Replayable's verdict.

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | The agent itself failed |
| 2 | Replay mismatch — behaviour diverged from the recording |
| 3 | Harness error — environment, configuration, or a bug in Replayable |
