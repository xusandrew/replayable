# Replayable

Replayable is a record/replay harness for HTTP-based workloads running in
Docker. It records traffic through a host-side mitmproxy, stores a redacted and
versioned cassette, and later serves recorded responses without contacting the
original servers.

The runtime and evaluation tooling for Milestones 0–6 are implemented:

- transparent HTTP and HTTPS recording for unmodified containers;
- structurally offline replay from the mitmproxy request hook;
- versioned, inspectable cassette bundles with content-addressed blobs;
- write-time secret redaction;
- SSE chunk recording and replay;
- normalized JSON request matching with volatile-field handling;
- project-specific matching rules;
- FIFO response ordering;
- structured mismatch reports and strict unconsumed-flow detection.
- record/replay clock pinning and stable Python hashing;
- replay by immutable image digest;
- deterministic workspace archives, hashes, and file-level diffs;
- dummy replay credentials, structured run logs, and transcript comparison;
- a real Anthropic research agent using Hacker News and Open-Meteo tools;
- 100-run determinism and recorded-cost benchmark scripts.

Milestone 5/6 acceptance evidence is still pending because it must be generated
from a real recording owned by your Anthropic account. See
[Generate the evaluation results](#generate-the-evaluation-results).

## Mental model

A normal record/replay cycle looks like this:

```text
record
  replayable CLI
    ├── starts mitmdump with the recording addon
    ├── starts your Docker container with proxy + CA settings
    ├── records each completed HTTP flow into flows.jsonl
    ├── captures stdout/stderr and a structured run log
    └── snapshots /workspace and finalizes manifest.json

replay
  replayable CLI
    ├── loads and normalizes the recorded requests
    ├── starts mitmdump with the replay addon
    ├── starts the exact recorded image digest with its clock pinned
    ├── matches live requests against per-key FIFO queues
    ├── synthesizes responses before mitmproxy can dial upstream
    └── compares workspace and transcript hashes
```

The workload does not import Replayable and does not need a Replayable SDK. It
only needs to:

1. run in Docker;
2. use HTTP or HTTPS for the external interactions being recorded;
3. trust the mounted mitmproxy CA;
4. honor the injected proxy environment variables.



## Prerequisites

Install:

- Python 3.12 or newer;
- [uv](https://docs.astral.sh/uv/);
- Docker Engine 20.10+ or Docker Desktop;
- network access for the initial record run.

From the repository root, install the locked Python environment:

```sh
uv sync --locked
```

Confirm the CLI and Docker daemon are available:

```sh
uv run replayable --help
docker info
```



### One-time mitmproxy CA setup

Start mitmdump once, wait until it says the proxy is listening, and stop it
with Ctrl-C:

```sh
uv run mitmdump
```

This generates:

```text
~/.mitmproxy/mitmproxy-ca-cert.pem
```

Replayable mounts this file read-only at `/etc/replayable/ca.pem` and sets:

- `SSL_CERT_FILE`;
- `REQUESTS_CA_BUNDLE`;
- `CURL_CA_BUNDLE`;
- `NODE_EXTRA_CA_CERTS`.

If the certificate is missing, Replayable exits with code 3 and tells you to
run mitmdump once.

## Research-agent quickstart

The full demo takes five commands after prerequisites. First create
`demo/research_agent/.env` from its example and replace the placeholder with
your `ANTHROPIC_API_KEY`.

```sh
docker build -t replayable/agent-base:local images/agent-base
docker build -t replayable/research-agent:local demo/research_agent
uv run replayable record --image replayable/research-agent:local \
  --env-file demo/research_agent/.env --out cassettes/research-agent \
  -- python /app/agent.py "deterministic replay for LLM agents"
uv run replayable inspect --cassette cassettes/research-agent
uv run replayable replay --cassette cassettes/research-agent --strict
```

Only `record` contacts Anthropic, Hacker News, or Open-Meteo. Replay requires
no env file or real API key. It runs the exact recorded image digest, freezes
the wall clock, injects `ANTHROPIC_API_KEY=[REDACTED:ANTHROPIC_API_KEY]`, and should print:

```text
DETERMINISTIC ✓ (workspace sha256 matches)
```

The replay also requires byte-identical agent stdout. For the strongest demo,
disconnect the host after recording.

For a keyless plumbing smoke test instead, build
`images/agent-base` and run `replayable-curl-workload`; the acceptance test is
`tests/e2e/test_m1_curl.py`.

## Capability guide

Each section describes what a feature does, what unlocks it, and how to
demonstrate it.

### Transparent container traffic recording

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

### Structurally offline replay

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

### Versioned, human-inspectable cassettes

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



### Inline bodies and content-addressed blobs

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



### Write-time secret redaction

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

### Normalized request matching

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

### Per-project normalization overrides

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



### Match explanations

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



### FIFO response ordering

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



### Structured mismatch diagnostics

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



### Unconsumed-flow reporting and strict mode

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


### Fork / hybrid replay

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



### SSE recording and replay

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



### Environment-file pass-through

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

### Deterministic workspace and transcript verification

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

### Time, hash-seed, and image pinning

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

### Stable process exit codes

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



## Command reference



### Record

```text
replayable record --image IMAGE
                  [--workspace DIR]
                  [--env-file FILE]
                  [--out CASSETTE_DIR]
                  [--port PORT]
                  [--ca-path FILE]
                  [--timeout SECONDS]
                  -- COMMAND [ARGS...]
```

`--port 0` picks a free ephemeral port so concurrent runs on one machine do
not collide. `--timeout` kills the container after the given number of
seconds, which guards against workloads that hang on frozen wall-clock
deadlines.

Example:

```sh
uv run replayable record \
  --image replayable/agent-base:local \
  --workspace ./workspace \
  --env-file ./.env \
  --out ./cassettes/example \
  -- python /app/agent.py "topic"
```

If `--out` is omitted, the output directory is `./cassette`.

### Replay

```text
replayable replay --cassette CASSETTE_DIR
                  [--strict]
                  [--fork-at N]
                  [--env-file FILE]
                  [--out-workspace DIR]
                  [--allow-image-mismatch]
                  [--port PORT]
                  [--ca-path FILE]
                  [--timeout SECONDS]
```

Replay uses the exact image identity and command from the cassette manifest.
Without `--out-workspace`, replay uses a fresh temporary directory. Only a
`replayable.toml` stored inside the cassette affects matching; a project-level
file in the current directory never changes how a recorded cassette replays.

### Inspect

```text
replayable inspect --cassette CASSETTE_DIR [--flow N]
replayable inspect [--cassette CASSETTE_DIR] --explain-match REQUEST_JSON
```

### Dashboard API

```sh
uv run replayable ui --cassette-root ./cassettes
```

The dashboard binds only to `127.0.0.1` and serves its API and packaged static
assets from one Python process. Read routes expose cassette summaries,
timelines, flow details, normalization explanations, mismatch/observation/diff
artifacts, and fork results. Start with `--allow-write` to enable JSON POST
actions for replay, fork, and recording a fresh named baseline:

```sh
uv run replayable ui --cassette-root ./cassettes --allow-write
```

Write actions reject non-JSON requests, cross-origin callers, non-loopback Host
headers, path traversal, concurrent mutations, and in-place baseline
replacement. A fresh baseline is recorded in hidden staging and published only
after a successful run.

The dashboard source lives in `ui/` and is a Vite-built React application. End
users do not need Node: the production build copies its assets into the Python
package. A completed fork is shown as pinned and live timeline segments, with
live model-call duration, cost, and stream-chunk evidence. Its downstream score
is deterministic and local: 60% multiset lexical overlap over the captured
stdout transcript, 25% LCS-aligned tool-call similarity, and 15% exact output
path/content/metadata overlap. The default pass threshold is 85%; this is not
an LLM or semantic judge, and the component scores remain visible for review.

Contributors can verify the complete UI with:

```sh
cd ui
pnpm install
pnpm test
pnpm lint
pnpm build
pnpm test:e2e
```

For live-data development, run the Python API on port 8765 and `pnpm dev` in
`ui/`; Vite proxies `/api` to that loopback server. Set
`REPLAYABLE_API_ORIGIN` only when using a different local API port.

Run any command with `--help` for the generated Typer documentation:

```sh
uv run replayable record --help
uv run replayable replay --help
uv run replayable inspect --help
uv run replayable ui --help
```



## Codebase guide



### Top-level layout

```text
.
├── src/replayable/                 Python package
├── images/agent-base/              Demo/test workload image
├── demo/research_agent/             Anthropic + two-tool agent image
├── scripts/                         Determinism and cost benchmarks
├── docs/limitations.md              Reproducible MVP limitations
├── tests/                          Unit and integration tests
├── .github/workflows/ci.yml        Lint, coverage, and Docker E2E CI
├── CHANGELOG.md
├── replayable-mvp-implementation-spec.md
├── pyproject.toml                  Package and tool configuration
├── uv.lock                         Locked Python dependency graph
└── README.md
```



### Runtime modules

`src/replayable/cli.py`

- Defines the Typer application.
- Owns the `record`, `replay`, and `inspect` command signatures.
- Converts runner failures into stable exit codes and user-facing messages.
- This is the entry point behind the `replayable` console command.

`src/replayable/runner.py`

- Orchestrates mitmdump and Docker subprocesses.
- Binds the proxy to the narrowest reachable interface and polls readiness,
guaranteeing SIGTERM teardown.
- Builds the Docker proxy, CA, workspace, and env-file contract.
- Creates and finalizes manifests.
- Loads and validates normalization rules.
- Reads replay state and reports, implements strict mode, and prints mismatch
summaries.

`src/replayable/inspection.py`

- Implements cassette inspection and match explanations behind the `inspect`
CLI command.

`src/replayable/cassette.py`

- Defines cassette version 1.0 and bundle paths.
- Reads and atomically writes `manifest.json`.
- Appends and recovers `flows.jsonl`.
- Stores inline bodies or content-addressed blobs.
- Verifies blob hashes and rejects incompatible major versions.
- Computes secret-safe environment fingerprints.

`src/replayable/redact.py`

- Classifies secret environment variables by name convention and by
URL-embedded credentials in values.
- Parses Docker-style env files.
- Redacts sensitive headers while preserving order and duplicates.
- Replaces literal secret body values before storage.

`src/replayable/normalize_rules.py`

- Contains the default volatile field names and value patterns as data.
- Loads and validates `replayable.toml`.
- Merges defaults, additions, and preserve rules.
- Computes the deterministic effective ruleset hash.

`src/replayable/matcher.py`

- Defines raw and normalized request representations.
- Canonicalizes hosts, queries, and JSON bodies.
- Builds SHA-256 match keys.
- Builds per-key FIFO queues from cassette flows.
- Tracks consumed and unconsumed sequences.
- Ranks diagnostic candidates and creates unified body diffs.

`src/replayable/addons/record_addon.py`

- Runs inside the host mitmdump process during record.
- Captures request and response data in the response hook.
- Enables streamed response capture for SSE in `responseheaders`.
- Applies redaction, body representation, hashing, sequencing, and timing.
- Appends each completed flow immediately.

`src/replayable/addons/replay_addon.py`

- Runs inside the host mitmdump process during replay.
- Loads the cassette and matcher.
- Converts live mitmproxy requests into matcher inputs.
- Synthesizes recorded responses in the request hook.
- Streams recorded SSE chunks.
- Writes structured mismatch reports and unconsumed-flow state.

`src/replayable/exit_codes.py`

- Defines the stable `ExitCode` enum shared by the CLI and runner.

`src/replayable/snapshot.py`

- Produces byte-stable gzip/tar workspace archives.
- Hashes regular files and symlink targets.
- Produces added/removed/changed path diagnostics.

`src/replayable/__init__.py`

- Exposes the current harness version used in cassette manifests.



### Image files

`images/agent-base/Dockerfile`

- Starts from `python:3.12-slim`.
- Installs curl, CA certificates, and libfaketime.
- Makes the documented libfaketime path available on both amd64 and arm64.
- Installs the curl acceptance workload.

`images/agent-base/curl_workload.sh`

- Performs the three GitHub requests and one httpbin POST.
- Acts as the M1–M3 smoke workload.



### Tests

`tests/test_exit_codes.py`

- CLI exit-code and dispatch behavior.

`tests/test_runner.py`

- Docker command construction, proxy lifecycle, strict mode, manifest rules,
inspect output, and match explanation behavior.

`tests/test_cassette.py`

- Bundle round trips, blob thresholds, truncation recovery, version checks,
malformed bundles, and body integrity.

`tests/test_redact.py`

- Header/body redaction and env-file parsing.

`tests/test_matcher.py`

- The matcher correctness kernel: normalization, volatile fields, overrides,
FIFO behavior, diffs, and idempotence.

`tests/test_m1_addons.py`

- Direct mitmproxy addon tests for recording, replay, SSE, redaction, and 599
responses. The historical filename is retained even though it now covers the
M2/M3 addon behavior.

`tests/e2e/test_m1_curl.py`

- Full Docker curl record/replay and missing-flow test.

`tests/e2e/test_m2_bundle.py`

- Local SSE origin, origin-offline replay, and whole-cassette secret scan.

`tests/e2e/test_m3_matcher.py`

- Five-request UUID/timestamp torture test and changed-prompt negative test.



### How the modules call each other

```text
cli.py
  └── runner.py
        ├── cassette.py
        ├── redact.py
        ├── normalize_rules.py
        ├── matcher.py
        └── mitmdump subprocess
              ├── addons/record_addon.py
              └── addons/replay_addon.py
```

The addons run in a separate mitmdump process, so the runner communicates with
them through:

- environment variables for cassette, rules, report, and state paths;
- append-only cassette files;
- `replay-state.json`;
- `replay-report.json`.



## Generate the evaluation results

After recording and successfully replaying the research agent, run the
100-replay proof:

```sh
uv run python scripts/prove_determinism.py \
  --cassette cassettes/research-agent \
  --runs 100
```

This writes `results/determinism.json` and fails if either workspace or stdout
has more than one observed hash. Then generate the latency/cost comparison:

```sh
uv run python scripts/benchmark.py \
  --cassette cassettes/research-agent
```

This writes `results/benchmark.json` and `results/benchmark.md`. The built-in
Claude Haiku 4.5 rates are $1/input MTok and $5/output MTok, verified against
Anthropic's published pricing on 2026-07-18. For another model, pass
`--input-price-per-million` and `--output-price-per-million`.

These result files are intentionally not fabricated in the repository: they
must be generated from the real cassette used in the evaluation.

## Development and testing

Run the fast unit suite:

```sh
uv run pytest
```

Run lint:

```sh
uv run ruff check .
```

Run all Docker-backed acceptance tests:

```sh
docker build -t replayable/agent-base:local images/agent-base
REPLAYABLE_RUN_E2E=1 uv run pytest tests/e2e -v
```

Run cassette and redaction branch coverage:

```sh
uv run pytest tests/test_cassette.py tests/test_redact.py \
  --cov=replayable.cassette \
  --cov=replayable.redact \
  --cov-branch \
  --cov-report=term \
  --cov-fail-under=90
```

Run matcher branch coverage:

```sh
uv run pytest tests/test_matcher.py \
  --cov=replayable.matcher \
  --cov-branch \
  --cov-report=term \
  --cov-fail-under=90
```

The GitHub Actions workflow runs:

1. locked dependency installation;
2. Ruff;
3. unit tests;
4. cassette/redaction coverage;
5. matcher coverage;
6. mitmproxy CA generation;
7. demo image build;
8. all Docker E2E tests.



## Troubleshooting



### `mitmproxy CA not found`

Run:

```sh
uv run mitmdump
```

Wait for startup, stop it with Ctrl-C, and verify:

```sh
test -f ~/.mitmproxy/mitmproxy-ca-cert.pem
```



### TLS certificate errors inside the workload

Confirm the client honors the injected CA variable. Curl specifically uses
`CURL_CA_BUNDLE`; Requests uses `REQUESTS_CA_BUNDLE`; many OpenSSL clients use
`SSL_CERT_FILE`.

Certificate-pinning clients cannot be transparently intercepted.

### No flows were recorded

Check:

1. the workload actually used HTTP or HTTPS;
2. its client honored `HTTP_PROXY` or `HTTPS_PROXY`;
3. it did not bypass the proxy through a custom transport;
4. `proxy.log` contains the expected host.

Curl intentionally ignores uppercase `HTTP_PROXY` for plain HTTP URLs. The
included demo uses HTTPS. For a plain-HTTP curl debugging workload, pass
`--proxy "$HTTPS_PROXY"` explicitly.

### Proxy port 8080 is already in use

The CLI defaults to port 8080. Pass `--port 0` to pick a free ephemeral port
(or `--port N` for a specific one), or stop the conflicting process.

### Docker exits 125

Replayable treats Docker exit 125 as a harness failure. Verify:

- Docker Desktop or Docker Engine is running;
- the image exists locally;
- the image name is correct;
- mounted paths are shared with Docker Desktop.



### Replay exits 2

Inspect:

```sh
python -m json.tool ./cassettes/your-cassette/replay-report.json
python -m json.tool ./cassettes/your-cassette/replay-state.json
```

Use `inspect --explain-match` to compare canonical requests. Do not solve a real
prompt or control-flow change by indiscriminately marking fields volatile.

### Normalization rules do not match the manifest

The cassette's `replayable.toml` changed or is missing. Restore the exact file
used during recording or record a new cassette. Replay intentionally refuses
to use a different ruleset silently.

### Replay exits 1 without a mismatch report

The container command itself failed before making a mismatched HTTP request.
Run the image and command manually, then inspect `replay-proxy.log`.

## Security model

Replayable's current security guarantees are deliberately narrow:

- configured auth headers are redacted at write time;
- secret-classified env values (name convention or URL-embedded credentials)
are replaced in request/response bodies and captured stdout/stderr;
- secret values reach the recording addon through a private 0600 temp file,
never through the process environment;
- secret values are not included in the environment fingerprint;
- the proxy binds to the loopback interface on Docker Desktop hosts and to the
Docker bridge gateway on native Linux, not to every interface;
- replay never forwards an unmatched request upstream;
- replay does not require the record-time env file.

Replayable does not currently:

- discover arbitrary secrets that follow neither the env-name convention nor
the URL-credential pattern;
- redact secrets embedded in commands, image layers, or proxy logs;
- classify proprietary response content;
- encrypt cassette bundles;
- provide access control for cassette directories;
- isolate the proxy from other containers on the same Docker bridge (Linux).

Review cassette contents before sharing them.

## Supported clients

- Python HTTPX: tested for ordinary HTTPS and Anthropic SSE streams.
- Python urllib/Requests: proxy and CA environment contract supported; urllib
is exercised by the single-call demo.
- curl: tested for HTTPS GET/POST and SSE; plain HTTP curl requires an explicit
proxy because curl ignores uppercase `HTTP_PROXY`.
- node-fetch: proxy/CA environment behavior is client-version dependent and is
not yet covered by CI.
- Go `net/http`: HTTP replay is possible for clients configured to honor the
proxy, but statically linked Go binaries do not support the time channel.



## Current limitations

Milestones 0–4 pass the automated suite; M5/M6 code is present, but producing
the checked acceptance evidence still requires one real Anthropic record and
the local 100-run proof described above.
Create the `v0.1.0` git tag only after those generated results have been
reviewed and committed.

Explicit MVP constraints:

- no non-Docker runtime;
- no non-HTTP protocol recording;
- no certificate-pinning bypass;
- no concurrency-tolerant matching for simultaneous identical requests;
- no RNG syscall interception;
- no fallback to approximate response serving;
- no static-binary time pinning.

The current matcher is intentionally strict: a near candidate helps explain a
failure but is never treated as a valid match.

See [docs/limitations.md](docs/limitations.md) for certificate pinning, static
Go time, non-HTTP traffic, and concurrent-identical-request reproductions.
