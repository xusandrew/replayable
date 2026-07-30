# Changelog

## 0.1.0 - Unreleased

- Record and structurally offline replay HTTP(S) and SSE traffic.
- Store redacted, versioned, content-addressed cassette bundles.
- Normalize volatile request fields and match repeated requests by FIFO.
- Pin replay time, Python hash seed, and the exact container image.
- Snapshot and compare deterministic workspaces with file-level diagnostics.
- Capture agent transcripts and structured run logs.
- Add the Anthropic research-agent demo and determinism/benchmark scripts.
- Bind the proxy to the loopback/bridge interface instead of all interfaces,
  pass secret values to the recorder via a private 0600 file, classify
  URL-embedded credentials (e.g. `postgres://user:pass@host`) as secrets, and
  allow `[secrets] names` overrides in `replayable.toml`.
- Redact live console mirrors as well as on-disk transcripts; inject
  `[REDACTED:NAME]` (not a separate dummy string) as replay secret values so
  body-auth and echoed secrets stay deterministic.
- Heal SSE chunks that split UTF-8 codepoints and store invalid UTF-8 chunks
  as base64.
- Record both the pullable repo digest and the immutable local image ID;
  enforce replay identity on the image ID.
- Detect a mitmproxy CA generated after the recorded epoch before replay.
- Add `--port` (0 = ephemeral), `--ca-path`, and `--timeout` to record/replay.
- Scope the environment fingerprint to the user-provided environment, write
  replay harness events to `replay.log`, remove stale replay artifacts when
  re-recording, and ignore a cwd `replayable.toml` during replay.
- Estimate prompt-cache token costs in the benchmark script.
- Add cassette v2 events, explicit replay policies, provider-neutral
  observations, and LCS-aligned structural tool-call diffs.
- Add secure fork/hybrid replay with live-segment cost and deterministic
  downstream comparison.
- Add the Vite-built local dashboard, packaged in the Python wheel and served
  with the loopback JSON API.
- Add reviewed, staged, atomic baseline replacement through `replayable accept`
  and the dashboard.
- Add a composite GitHub Action with PR comments, JUnit output, replay
  artifacts, cost-savings evidence, and baseline staleness warnings.
- Add a secret-gated scheduled live-drift workflow.
