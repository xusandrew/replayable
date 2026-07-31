# Command reference

Every command also accepts `--help` for generated Typer documentation.

## Record

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

## Replay

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

## Inspect

```text
replayable inspect --cassette CASSETTE_DIR [--flow N]
replayable inspect [--cassette CASSETTE_DIR] --explain-match REQUEST_JSON
```

## Dashboard API

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
headers, path traversal, concurrent mutations, and replacement of any cassette
other than the selected baseline. Every baseline is recorded and validated in
hidden staging before a new sibling is published or the selected baseline is
atomically replaced.

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

See the [dashboard guide](dashboard/README.md) for the complete
read-only, write-enabled, fork, baseline, and UI-development walkthrough.

## Accept a replacement baseline

`accept` records a complete candidate using the baseline's image and command,
prints bounded transcript/tool/workspace differences, and asks before changing
the cassette:

```sh
uv run replayable accept \
  --cassette ./cassettes/research-agent \
  --env-file ./demo/research_agent/.env
```

Recording or validation failure leaves the old directory untouched. Publication
uses a same-filesystem atomic rename with rollback.

Run any command with `--help` for the generated Typer documentation:

```sh
uv run replayable record --help
uv run replayable replay --help
uv run replayable accept --help
uv run replayable inspect --help
uv run replayable ui --help
uv run replayable doctor --help
```



