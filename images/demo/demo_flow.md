## Prerequisites

```sh
docker build \
  --tag replayable/anthropic-demo:local \
  images/demo

cp \
  images/demo/workspace/prompt-recorded.txt \
  images/demo/workspace/prompt.txt
```

## Running replayable

```sh

uv run replayable record \
  --image replayable/anthropic-demo:local \
  --workspace ./images/demo/workspace \
  --env-file ./images/demo/.env \
  --out ./cassettes/anthropic-demo \
  -- replayable-anthropic-demo
```

## Turn off wifi

```sh
uv run replayable replay \
  --cassette ./cassettes/anthropic-demo \
  --strict \
  --out-workspace ./images/demo/workspace
```

## 5. Change the prompt without rebuilding


```sh
cp \
  images/demo/workspace/prompt-changed.txt \
  images/demo/workspace/prompt.txt
```

```sh
uv run replayable replay \
  --cassette ./cassettes/anthropic-demo \
  --strict \
  --out-workspace ./images/demo/workspace
```
- Same recorded Claude interaction can be replayed offline.

- Agent code can be tested without model variability or API cost.

- Changing the prompt demonstrates that Replayable won’t incorrectly serve a cached response for a behaviorally different request.



