#!/bin/sh
set -eu

exec python - <<'PY'
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
PROMPT_PATH = Path("/workspace/prompt.txt")
MODEL_PATH = Path("/workspace/model.txt")
REPLAY_KEY = "replay-demo-placeholder"


def read_required(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"demo: cannot read {label} at {path}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not value:
        print(f"demo: {label} at {path} is empty", file=sys.stderr)
        raise SystemExit(1)
    return value


prompt = read_required(PROMPT_PATH, "prompt")
model = read_required(MODEL_PATH, "model")
api_key = os.environ.get("ANTHROPIC_API_KEY", REPLAY_KEY)
payload = json.dumps(
    {
        "model": model,
        "max_tokens": 160,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    },
    separators=(",", ":"),
).encode("utf-8")
request = urllib.request.Request(
    API_URL,
    data=payload,
    method="POST",
    headers={
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
        "x-api-key": api_key,
    },
)

try:
    with urllib.request.urlopen(request, timeout=60) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if not data or data == "[DONE]":
                continue
            event = json.loads(data)
            delta = event.get("delta", {})
            if (
                event.get("type") == "content_block_delta"
                and delta.get("type") == "text_delta"
            ):
                print(delta.get("text", ""), end="", flush=True)
                time.sleep(0.025)
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", errors="replace")
    print(f"demo: API request failed with HTTP {exc.code}: {detail}", file=sys.stderr)
    raise SystemExit(1) from exc
except (OSError, TimeoutError, json.JSONDecodeError) as exc:
    print(f"demo: request failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

print()
PY
