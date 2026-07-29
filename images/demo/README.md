# Real Anthropic record/replay demo

This image sends one streamed request to Anthropic's Messages API. Replayable
records the real server-sent event stream and can later replay it with no API
key and no network access.

The prompt and model live in the mounted `workspace/` directory, not in the
image. Changing `workspace/prompt.txt` therefore changes the request without
rebuilding the image.

Run every command below from the repository root.

## Files

```text
images/demo/
├── Dockerfile
├── .dockerignore
├── anthropic_workload.sh
├── .env.example
└── workspace/
    ├── model.txt
    ├── prompt.txt
    ├── prompt-recorded.txt
    └── prompt-changed.txt
```

- `anthropic_workload.sh` reads `/workspace/model.txt` and
  `/workspace/prompt.txt`, calls `POST /v1/messages` with `stream: true`, and
  prints Claude's text deltas.
- `prompt-recorded.txt` and `prompt-changed.txt` make the presentation sequence
  repeatable.
- `model.txt` defaults to the inexpensive `claude-haiku-4-5` model. Replace
  this file before recording if that model is unavailable to your account.

## Prerequisites

1. Docker is running.
2. The project environment is installed with `uv sync --locked`.
3. Replayable's mitmproxy CA exists at
   `~/.mitmproxy/mitmproxy-ca-cert.pem`. Generate it once with:

   ```sh
   uv run mitmdump
   ```

   Stop mitmdump after it creates the certificate.
4. Your Anthropic account can call the model named in
   `images/demo/workspace/model.txt`.

## 1. Add the API key

Create the ignored environment file:

```sh
cp images/demo/.env.example images/demo/.env
```

Replace the placeholder in `images/demo/.env`:

```text
ANTHROPIC_API_KEY=your-real-key
```

Do not add the key to the Dockerfile, shell script, command line, prompt, or
model file. `.env` files and `cassettes/` are ignored by this repository, and
`.dockerignore` prevents the local env file from entering the Docker build
context.

## 2. Build the image

```sh
docker build \
  --tag replayable/agent-base:local \
  images/agent-base

docker build \
  --tag replayable/anthropic-demo:local \
  images/demo
```

The prompt is not copied into the image, so later prompt changes do not require
another build.

## 3. Record the real Claude stream

Reset the active prompt, then record:

```sh
cp \
  images/demo/workspace/prompt-recorded.txt \
  images/demo/workspace/prompt.txt

uv run replayable record \
  --image replayable/anthropic-demo:local \
  --workspace ./images/demo/workspace \
  --env-file ./images/demo/.env \
  --out ./cassettes/anthropic-demo \
  -- replayable-anthropic-demo
```

This is the only step that contacts Anthropic. Perform it before the
presentation while the network and API are available.

Replayable redacts the `x-api-key` header before writing the cassette. The
workload uses a harmless placeholder key when `ANTHROPIC_API_KEY` is absent,
which is what happens during replay. Request headers are excluded from matching,
so the placeholder does not change the behavioral match key.

Treat the cassette as sensitive even after redaction: it contains the prompt
and Claude's response.

## 4. Inspect and test offline replay

Inspect the recorded artifact:

```sh
uv run replayable inspect \
  --cassette ./cassettes/anthropic-demo
```

Turn off Wi-Fi, ensure the original prompt is active, and replay:

```sh
cp \
  images/demo/workspace/prompt-recorded.txt \
  images/demo/workspace/prompt.txt

uv run replayable replay \
  --cassette ./cassettes/anthropic-demo \
  --strict \
  --out-workspace ./images/demo/workspace
```

The replay command does not receive `images/demo/.env`. The container sends the
same request with a Replayable-injected dummy key, mitmproxy matches the recorded request,
and the stored SSE response is returned before any upstream connection can be
made.

Replay also pins the exact recorded image and clock, compares stdout, and
verifies that the workspace hash matches.

Presentation line:

> This response is coming from a file, not from Anthropic—the Wi-Fi is off.

## 5. Change the prompt without rebuilding

Replace only the mounted prompt:

```sh
cp \
  images/demo/workspace/prompt-changed.txt \
  images/demo/workspace/prompt.txt
```

Run the same replay command:

```sh
uv run replayable replay \
  --cassette ./cassettes/anthropic-demo \
  --strict \
  --out-workspace ./images/demo/workspace
```

Expected result:

- the normalized request no longer matches because the prompt is behavioral;
- Replayable does not contact Anthropic or return the old answer;
- the process exits with code `2`;
- `cassettes/anthropic-demo/replay-report.json` contains the recorded-versus-live
  prompt diff.

Pretty-print the report if desired:

```sh
python -m json.tool \
  ./cassettes/anthropic-demo/replay-report.json
```

Restore the recorded prompt before the next rehearsal:

```sh
cp \
  images/demo/workspace/prompt-recorded.txt \
  images/demo/workspace/prompt.txt
```

## Presentation checklist

1. Record the cassette the night before.
2. Replay it successfully with Wi-Fi off.
3. Test the changed-prompt mismatch.
4. Restore `prompt-recorded.txt`.
5. Keep Docker running and the demo image available locally.
6. Keep a screen recording of the successful replay and mismatch as a backup.
7. Keep the recorded image digest available locally; Replayable refuses a
   mutable replacement by default.

## Troubleshooting

### Recording returns HTTP 401

The real key is missing or invalid. Confirm `images/demo/.env` contains
`ANTHROPIC_API_KEY` and that `--env-file` is present on the record command.

### Recording returns a model error

Use Anthropic's Models API or account console to choose an available model,
update `images/demo/workspace/model.txt`, and record a new cassette. Do not
change the model between record and replay.

### Replay cannot read the prompt

Pass `--out-workspace ./images/demo/workspace`. Record's `--workspace` path is
not automatically reused by replay.

### Replay exits 1 without a mismatch report

The workload failed before mitmproxy observed a mismatched request. Confirm the
workspace files exist, the image is available, and the mitmproxy CA is present.

### Port 8080 is already in use

Stop the process using port 8080 before recording or replaying.
