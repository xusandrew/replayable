# Using the Replayable dashboard

The dashboard is a local run explorer for recorded cassettes. Vite builds the
React source during development, but the generated HTML, JavaScript, and CSS
ship inside the Python package. Running the app requires only the normal
Replayable Python and Docker prerequisites.

## Open the checked-in demo

From the repository root:

```sh
uv sync --locked
uv run replayable ui \
  --cassette-root tests/fixtures/cassettes \
  --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). This mode is read-only.
You can browse cassettes, inspect the timeline, see the first mismatch, review
normalization rules, and inspect hybrid-replay evidence.

To browse your own recordings:

```sh
uv run replayable ui --cassette-root ./cassettes
```

The server binds to `127.0.0.1`; it is not exposed to the LAN.

## Enable actions

Replay, fork, and baseline buttons are disabled by the API unless the server is
started explicitly with write access:

```sh
uv run replayable ui \
  --cassette-root ./cassettes \
  --allow-write
```

- **Replay** is offline and does not need credentials.
- **Replay fork** serves the selected prefix from the cassette and then resumes
  live traffic. Supply the environment-file path in the dialog.
- **Re-record baseline** records a complete hidden candidate and atomically
  replaces the selected cassette only after recording and validation succeed.
- **Save as new baseline** publishes a sibling cassette and refuses to
  overwrite an existing name.

Write requests must be same-origin JSON requests with a loopback `Host` header.
Only one mutation runs at a time.

## Accept a baseline from the CLI

The CLI always shows the process, transcript, workspace, and tool-call changes
before replacing anything:

```sh
uv run replayable accept \
  --cassette ./cassettes/research-agent \
  --env-file ./demo/research_agent/.env
```

Answer `y` only after reviewing the candidate. `--yes` is available for an
already-reviewed automation, but it intentionally does not suppress the diff.
The old baseline remains in place if recording, validation, or publication
fails.

## Develop the React UI

Run the Python API:

```sh
uv run replayable ui \
  --cassette-root ./cassettes \
  --allow-write \
  --port 8765
```

In a second terminal:

```sh
cd ui
corepack enable
pnpm install --frozen-lockfile
pnpm dev
```

Open the URL printed by Vite. It proxies `/api` to
`http://127.0.0.1:8765`. Set `REPLAYABLE_API_ORIGIN` before `pnpm dev` only
when the Python server uses a different local port.

Validate a production change with:

```sh
pnpm test
pnpm lint
pnpm build
pnpm test:e2e
```

`pnpm build` replaces `src/replayable/ui_static` with the production bundle;
those generated assets are intentionally committed because the wheel does not
run a Node build.

## Reference screenshots

- [Offline mismatch explorer](../screenshots/dashboard-screen-a.png)
- [Hybrid replay and downstream check](../screenshots/dashboard-screen-b.png)
