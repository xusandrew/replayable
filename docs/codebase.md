# Codebase guide

## Top-level layout

```text
.
├── src/replayable/                 Python package
├── ui/                             Vite + React dashboard source
├── actions/github/                 Composite PR-verdict action
├── images/agent-base/              Demo/test workload image
├── demo/research_agent/            Anthropic + two-tool agent image
├── scripts/                        CI, determinism, and cost utilities
├── docs/README.md                  Documentation index
├── tests/                          Unit and integration tests
├── .github/workflows/              CI, replay, and live drift jobs
├── CHANGELOG.md
├── replayable-mvp-implementation-spec.md
├── pyproject.toml                  Package and tool configuration
├── uv.lock                         Locked Python dependency graph
└── README.md
```



## Runtime modules

| Path | Responsibility |
|---|---|
| `cli.py` | Typer entry points and stable exit-code translation |
| `runner.py` | Compatibility façade for orchestration callers |
| `core/orchestrator.py` | Record, offline replay, and fork run lifecycles |
| `core/{docker,proxy,ca,container}.py` | Runtime boundary collaborators |
| `baseline.py` | Reviewed candidate recording and atomic baseline publication |
| `cassette/` | Versioned bundles, blobs, and the event log |
| `core/policy.py` | Deterministic channel/scope policy resolution |
| `matcher.py` | Request normalization, FIFO matching, and mismatch diagnostics |
| `verdict/` | Observations, structural diffs, usage, similarity, and fork results |
| `ui_server.py` | Loopback JSON API and packaged-dashboard server |
| `addons/` | Isolated mitmproxy record and replay processes |
| `snapshot.py` | Deterministic workspace archive and file-level comparison |
| `redact.py` | Secret classification, env parsing, and write-time redaction |



## Image files

`images/agent-base/Dockerfile`

- Starts from `python:3.12-slim`.
- Installs curl, CA certificates, and libfaketime.
- Makes the documented libfaketime path available on both amd64 and arm64.
- Installs the curl acceptance workload.

`images/agent-base/curl_workload.sh`

- Performs the three GitHub requests and one httpbin POST.
- Acts as the M1–M3 smoke workload.



## Tests

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



## How the modules call each other

```text
cli.py
  ├── runner.py compatibility façade
  │     └── core/orchestrator.py
  │           ├── core/{docker,proxy,ca,container}.py
  │           ├── cassette/ + core/policy.py
  │           ├── verdict/ + snapshot.py
  │           └── mitmdump subprocess
  │                 └── addons/{record,replay}_addon.py
  ├── baseline.py ──► core/orchestrator.record_run
  └── ui_server.py ──► runner.py + baseline.py

browser ──► ui_server.py /api
  └── packaged Vite assets in src/replayable/ui_static/
```

The addons run in a separate mitmdump process, so the runner communicates with
them through:

- environment variables for cassette, rules, report, and state paths;
- append-only cassette files;
- `replay-state.json`;
- `replay-report.json`.



