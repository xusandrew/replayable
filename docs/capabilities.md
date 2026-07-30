# Capability guide

Each section describes what a feature does, what unlocks it, and how to
demonstrate it. For a one-line summary of each, see the table in the
[project README](../README.md#what-it-does).

## Transparent container traffic recording

**Capability**

Replayable routes a container's HTTP and HTTPS traffic through mitmproxy
without modifying the workload's source code. The runner injects:

- `HTTP_PROXY` and `HTTPS_PROXY` pointing to
`host.docker.internal:<proxy-port>`;
- `NO_PROXY=localhost,127.0.0.1`;
- `--add-host=host.docker.internal:host-gateway`;
- the mitmproxy CA mount and client-specific CA variables;
- `--rm` and a unique `replayable-<run-id>` container name.

**Unlock it**

1. Generate the mitmproxy CA.
2. Use a client that honors proxy and CA environment variables.
3. Make the required client executable available in your image.

**Demo it**

```sh
REPLAYABLE_RUN_E2E=1 \
  uv run pytest tests/e2e/test_m1_curl.py -s
```

This records the four-request curl workload, verifies the four expected flows,
replays them, removes one flow, and verifies mismatch exit code 2.

## Structurally offline replay

**Capability**

The replay addon attaches `flow.response` in mitmproxy's `request` hook. A
matched request therefore has no upstream connection path. An unmatched
request also receives a synthetic response; it is never forwarded as a
fallback.

The proxy also runs with `connection_strategy=lazy`, preventing eager upstream
connections while mitmproxy is waiting for the request.

**Unlock it**

Record a cassette successfully, then use `replayable replay`.

**Demo it**

```sh
REPLAYABLE_RUN_E2E=1 \
  uv run pytest tests/e2e/test_m2_bundle.py -s
```

The test records against a local origin server, stops that server, and then
successfully replays the cassette. This proves replay is not using the origin.

## Versioned, human-inspectable cassettes

**Capability**

A cassette is a directory rather than an opaque database. The core files are:

```text
cassette/
├── manifest.json
├── flows.jsonl
├── blobs/
├── workspace.tar.gz
├── workspace.sha256
├── workspace.files.json
├── agent.stdout
├── agent.stderr
├── run.log
├── replayable.toml          # present when project overrides were recorded
├── proxy.log
├── replay.log               # harness events for the most recent replay
├── replay-proxy.log         # created by replay
├── replay-agent.stdout      # most recent replay transcript
├── last-replay.json         # hashes, wall time, and final status
├── replay-state.json        # current unconsumed-flow state
└── replay-report.json       # created only after a mismatch
```

Recording into an existing cassette directory removes any replay artifacts
left by earlier runs, so a re-recorded cassette never carries a stale mismatch
report.

`manifest.json` includes:

- cassette and harness versions;
- creation time and recording epoch;
- image reference, pullable repo digest, and immutable local image ID;
- recorded command;
- environment fingerprint;
- redacted-header policy;
- flow count;
- normalization ruleset hash;
- exact image digest and environment variable names;
- record wall time plus workspace/stdout/stderr hashes.

`flows.jsonl` is append-only during recording. Each line contains one complete
flow with sequence number, request key, headers, body representation, hashes,
response, SSE chunks when applicable, and relative timing.

The loader detects and drops only an incomplete final JSONL line. Invalid
complete lines remain hard errors.

**Unlock it**

No extra configuration is required. Every record command produces this format.

**Demo it**

```sh
uv run replayable inspect --cassette ./cassettes/curl-demo
uv run replayable inspect --cassette ./cassettes/curl-demo --flow 4
```

The storage round-trip, version gate, and truncated-line behavior are covered
by:

```sh
uv run pytest tests/test_cassette.py -v
```



## Inline bodies and content-addressed blobs

**Capability**

Replayable stores valid UTF-8 bodies of at most 256 KiB directly in
`flows.jsonl` as `inline_utf8`. It stores binary bodies and larger bodies under:

```text
blobs/<sha256>
```

Identical large bodies share the same blob automatically. Blob hashes are
verified when read.

**Unlock it**

This behavior is automatic and depends only on body size and UTF-8 validity.

**Demo it**

```sh
uv run pytest \
  tests/test_cassette.py::test_blob_spill_threshold_binary_spill_and_deduplication \
  -v
```



## Write-time secret redaction

**Capability**

Secrets are redacted before request or response data is hashed or written.

The following headers are always replaced with `[REDACTED]`:

- `authorization`;
- `x-api-key`;
- `api-key`;
- `cookie`;
- `set-cookie`.

Environment variables whose names contain `KEY`, `TOKEN`, `SECRET`, or
`PASSWORD` are classified as secrets, as are variables whose values contain
URL-embedded credentials such as `postgres://user:password@host/db`. You can
also force classification of specific names in `replayable.toml`:

```toml
[secrets]
names = ["DATABASE_URL", "CUSTOM_CREDENTIAL"]
```

Literal occurrences of secret values in request or response bodies become:

```text
[REDACTED:VARIABLE_NAME]
```

The same streaming redactor is applied before captured agent stdout/stderr is
written, including when a secret crosses a pipe-read boundary. Secret values
reach the recorder through a private `0600` tempfile, not the process
environment.

The environment fingerprint includes only the variable name for
secret-classified variables, never the value.

**Unlock it**

Pass credentials through `--env-file` rather than embedding them in the image,
command, URL, or ordinary non-secret variables:

```sh
uv run replayable record \
  --image your-image:local \
  --env-file ./.env \
  --out ./cassettes/with-secrets \
  -- your-command
```

Example `.env`:

```dotenv
ANTHROPIC_API_KEY=replace-with-real-value
DATABASE_PASSWORD=replace-with-real-value
DATABASE_URL=postgres://user:password@db.internal/app
```

**Demo it**

```sh
REPLAYABLE_RUN_E2E=1 \
  uv run pytest tests/e2e/test_m2_bundle.py -s
```

The test sends a fake token in an authorization header, cookie, request body,
and response body, then scans every cassette file to prove the original token
does not appear.

Redaction does not make cassettes public. Non-secret response bodies may still
contain proprietary or personal data, so treat cassette directories as
sensitive artifacts.

## Normalized request matching

**Capability**

Replayable normalizes both recorded and live requests before matching:

1. uppercase the method;
2. lowercase the host and remove default ports;
3. parse and sort query parameters by key;
4. parse JSON bodies and replace volatile values;
5. serialize JSON canonically with sorted keys and compact separators;
6. use the body SHA-256 unchanged for non-JSON or invalid JSON;
7. hash `method\nhost\npath\nquery\ncanonical_body`.

Headers are excluded from matching. This allows authorization values, client
versions, tracing headers, and user agents to vary without changing behavior.
The content type is still consulted to decide whether a body is JSON.

Default volatile field names, at any nesting depth, are:

- `id`;
- `request_id`;
- `tool_call_id`;
- `tool_use_id`;
- `call_id`;
- `trace_id`;
- `span_id`;
- `idempotency_key`;
- `nonce`;
- `created`;
- `created_at`;
- `timestamp`.

Remaining string values are normalized when they are:

- UUID v4 values;
- ISO-8601 datetimes;
- 10- or 13-digit epoch strings under keys containing `time`, `date`, or `ts`.

Everything else remains behavioral. Changing a prompt changes the match key.

**Unlock it**

Normalization is automatic during replay. JSON requests must carry an
`application/json` content type to use JSON normalization.

**Demo it**

```sh
uv run pytest tests/test_matcher.py -v
```

The suite covers JSON key order, query order, UUIDs, timestamps, nested fields,
headers, floats, prompt changes, FIFO ordering, overrides, and normalization
idempotence.

The full five-request torture demo is:

```sh
REPLAYABLE_RUN_E2E=1 \
  uv run pytest tests/e2e/test_m3_matcher.py -s
```

It records five POSTs with fresh UUIDs and timestamps, stops the origin, then
replays five distinct responses in their original order.

## Per-project normalization overrides

**Capability**

Create `replayable.toml` in the working directory to add volatile field names,
add string regexes, or preserve a field that defaults would otherwise replace:

```toml
[normalization]
field_names = ["session_marker", "runtime_generated_value"]
regexes = ["^run-[0-9]+$", "^temporary_[a-f0-9]+$"]
preserve = ["id"]
```

During recording, Replayable:

1. loads the working-directory override;
2. merges it with the defaults;
3. hashes the effective rules;
4. stores the hash in `manifest.json`;
5. copies the file into the cassette.

During replay, the cassette copy takes precedence over a working-directory
file. If the effective hash differs from the manifest, replay exits 3 instead
of silently matching with different rules.

**Unlock it**

Add `replayable.toml` before recording. Record a new cassette whenever matching
rules intentionally change.

**Demo it**

```sh
uv run pytest \
  tests/test_matcher.py::test_override_adds_field_name_and_regex \
  tests/test_matcher.py::test_toml_preserve_override_keeps_default_field \
  tests/test_runner.py::test_record_pins_project_rules_in_cassette_manifest \
  -v
```



## Match explanations

**Capability**

`inspect --explain-match` shows the effective ruleset hash, canonical body,
pre-hash string, and final match key for a request description.

**Unlock it**

Create a request JSON file:

```json
{
  "method": "POST",
  "scheme": "https",
  "host": "api.example.com",
  "port": 443,
  "path": "/v1/messages",
  "query": "z=2&a=1",
  "headers": {
    "content-type": "application/json",
    "authorization": "Bearer ignored-for-matching"
  },
  "body": {
    "tool_call_id": "dynamic-value",
    "prompt": "Explain deterministic replay"
  }
}
```

Run:

```sh
uv run replayable inspect --explain-match ./request.json
```

To use the rules pinned in a cassette:

```sh
uv run replayable inspect \
  --cassette ./cassettes/curl-demo \
  --explain-match ./request.json
```

**Demo it**

```sh
uv run pytest \
  tests/test_runner.py::test_explain_match_renders_prehash_and_normalized_body \
  -v
```



## FIFO response ordering

**Capability**

Recorded flows are queued by normalized match key. Repeated identical requests
pop responses in recorded order. Replayable never serves a merely similar
candidate, because doing so would hide changed behavior.

**Unlock it**

No extra flag is required. Make repeated requests sequentially for the current
MVP concurrency model.

**Demo it**

```sh
uv run pytest \
  tests/test_matcher.py::test_identical_requests_pop_distinct_responses_fifo \
  tests/e2e/test_m3_matcher.py \
  -v
```



## Structured mismatch diagnostics

**Capability**

An unmatched request receives HTTP 599 with an
`application/json` response containing:

- the normalized live request;
- up to three nearest diagnostic candidates;
- a unified diff against the nearest canonical body.

The same payload is written to:

```text
cassette/replay-report.json
```

The CLI prints the method, path, and up to five diff hunks. Nearest candidates
are diagnostic only and are never served.

**Unlock it**

Replay a cassette with behavior that differs from the recording, such as a
changed prompt, path, query, or non-volatile body field.

**Demo it**

```sh
REPLAYABLE_RUN_E2E=1 \
  uv run pytest tests/e2e/test_m3_matcher.py -s
```

The second half of this test changes `same prompt` to `changed prompt`, expects
exit code 2, and verifies that both phrases appear in the report diff.

After a manual mismatch:

```sh
uv run replayable inspect --cassette ./cassettes/your-cassette
python -m json.tool ./cassettes/your-cassette/replay-report.json
```



## Unconsumed-flow reporting and strict mode

**Capability**

The replay addon writes the remaining recorded sequence numbers to
`replay-state.json` after each match and at shutdown.

- Default mode prints a warning and can still exit 0.
- `--strict` returns exit code 2 when any flows remain.

This catches workloads that unexpectedly stop early while allowing deliberate
short-circuiting during exploratory replay.

**Unlock it**

Pass `--strict` when every recorded interaction is expected:

```sh
uv run replayable replay \
  --cassette ./cassettes/your-cassette \
  --strict
```

**Demo it**

```sh
uv run pytest tests/test_runner.py::test_replay_reports_unconsumed_flows -v
```


## Fork / hybrid replay

`--fork-at N` serves recorded flows `[0, N)` and then resumes upstream
network access. The original cassette remains immutable; the run writes
`fork-result.json` with pinned/live counts, live usage and cost, timing, and
downstream transcript, workspace, exit-code, and tool-sequence comparisons.

Live forks require an environment file containing every secret name recorded
in the cassette. Replayable rejects new variables and changed non-secret
values, passes secrets through a private file to the redacting recorder, and
never stores their literal values:

```sh
uv run replayable replay \
  --cassette ./cassettes/your-cassette \
  --fork-at 3 \
  --env-file ./.env
```

`N=0` makes every request live. `N=flow_count` still requires credentials:
an unexpected request after the recorded end is live by definition.



## SSE recording and replay

**Capability**

Responses whose content type begins with `text/event-stream` are streamed
through the recording addon. Each callback chunk is stored separately as:

```json
{"data_utf8": "event: message\ndata: ...\n\n"}
```

A transport chunk boundary that splits a multi-byte UTF-8 character is healed
by carrying the incomplete bytes into the next chunk; genuinely invalid UTF-8
is stored as `{"data_base64": "..."}`. The concatenated bytes are preserved
exactly in both cases.

Replay verifies the concatenated body hash, then uses a streaming transformer
to emit the recorded chunks in order without artificial delay. An empty final
callback closes the response cleanly.

**Unlock it**

The origin must return `Content-Type: text/event-stream`. No Replayable flag is
required. The client should use its normal streaming mode; for curl, use `-N`
or `--no-buffer`.

**Demo it**

```sh
REPLAYABLE_RUN_E2E=1 \
  uv run pytest tests/e2e/test_m2_bundle.py -s
```

The test records a finite local SSE stream, verifies the stored chunk
concatenation and hash, stops the origin, and replays it to completion.

The callback-level replay behavior is covered by:

```sh
uv run pytest \
  tests/test_m1_addons.py::test_sse_stream_callback_preserves_chunks_and_hashes_concatenation \
  -v
```



## Environment-file pass-through

**Capability**

`record --env-file FILE` passes a Docker-style environment file into the
recording container. Replayable parses the same file on the host to classify
secret values and calculate the environment fingerprint.

Replayable's proxy and CA variables override conflicting values from the file.

**Unlock it**

```sh
uv run replayable record \
  --image your-image \
  --env-file ./.env \
  --out ./cassettes/run \
  -- your-command
```

**Demo it**

```sh
uv run pytest tests/test_redact.py -v
```

Replay stores non-secret values and only the names of secret-classified
variables. It restores the non-secret configuration and injects
`[REDACTED:VARIABLE_NAME]` for each secret during replay — the same token
record wrote into cassette bodies and transcripts — so body-auth APIs and
echoed secrets stay matchable without real credentials or the record-time env
file.

## Deterministic workspace and transcript verification

**Capability**

Record always mounts a workspace at `/workspace`. If `--workspace` is omitted,
Replayable creates a temporary empty directory. At exit it writes a
deterministic tarball, its SHA-256, and a per-file manifest:

```sh
mkdir -p ./workspace

uv run replayable record \
  --image your-image \
  --workspace ./workspace \
  --out ./cassettes/run \
  -- your-command
```

Replay starts from a fresh empty workspace, creates the same deterministic
snapshot, and compares it with the recording. A match prints
`DETERMINISTIC ✓`; a mismatch returns exit 2 and lists added, removed, and
changed paths. When the only differences are missing recorded files, replay
prints a hint that the workload probably reads input files from `/workspace`
and needs a pre-seeded workspace. `--out-workspace` mounts a selected host
directory for that purpose; make sure it contains the same intentional inputs
as the record workspace:

```sh
mkdir -p ./replay-workspace

uv run replayable replay \
  --cassette ./cassettes/run \
  --out-workspace ./replay-workspace
```

Replay also compares `replay-agent.stdout` with the recorded `agent.stdout`
by SHA-256 and prints an informational warning when stderr drifts. JSONL
harness events are retained in `run.log` (record) and `replay.log` (replay)
for diagnosis.

**Demo it**

Use the research-agent quickstart, then inspect `workspace.files.json`,
`agent.stdout`, and `run.log` in its cassette.

## Time, hash-seed, and image pinning

Record and replay both set `PYTHONHASHSEED=0`. Record stores the pullable
Docker repo digest and the immutable local image ID. Replay enforces identity
on the image ID (the ground truth for "these exact bytes ran") and refuses to
run if that exact image is unavailable.

Immediately before record launch, Replayable captures real `t0`; both record
and replay then set:

```text
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1
FAKETIME=<recorded UTC timestamp>
FAKETIME_DONT_FAKE_MONOTONIC=1
```

Use `--allow-image-mismatch` only while developing a changed image. It uses
the recorded mutable tag and weakens the determinism claim.

Because replay pins the container clock to the recorded epoch, replay fails
fast with an actionable error if the mitmproxy CA certificate was generated
after the recording (for example on a new machine); the pinned clock would
otherwise see a confusing "certificate is not yet valid" TLS failure.

## Stable process exit codes

Replayable uses:

- `0`: success;
- `1`: the workload inside the container exited nonzero;
- `2`: replay mismatch or strict unconsumed flows;
- `3`: harness or infrastructure error.

This makes shell scripts and CI jobs independent of human-readable output.

**Demo it**

```sh
uv run pytest tests/test_exit_codes.py -v
```



