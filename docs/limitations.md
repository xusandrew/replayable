# MVP limitations and reproductions

Replayable is deliberately Docker- and HTTP-focused. Cassettes can contain
proprietary response data after credentials are redacted; treat them as
sensitive artifacts.

## Proxy listen interface

During every record/replay the mitmproxy process listens on the narrowest
host interface containers can still reach: loopback on Docker Desktop
(macOS/Windows), or the Docker bridge gateway on native Linux. If the bridge
gateway cannot be resolved, Replayable falls back to `0.0.0.0` and prints a
warning. On that fallback path, any peer that can reach the listen port can
send traffic through your TLS-intercepting proxy while a run is active—keep a
host firewall in place, or pass `--port` on an isolated network.

## Certificate-pinning clients

The proxy presents a mitmproxy certificate, not the origin's public key.
Clients that pin the origin key reject the connection before Replayable can
record it.

Reproduce by obtaining an origin's SPKI pin and then running curl through
mitmproxy:

```sh
PIN="$(
  openssl s_client -servername api.github.com -connect api.github.com:443 </dev/null 2>/dev/null |
    openssl x509 -pubkey -noout |
    openssl pkey -pubin -outform DER |
    openssl dgst -sha256 -binary |
    openssl base64 -A
)"
HTTPS_PROXY=http://127.0.0.1:8080 \
  curl --pinnedpubkey "sha256//$PIN" https://api.github.com/zen
```

The expected result is curl error 90 while mitmdump is running.

## Static binaries and time

`libfaketime` works through `LD_PRELOAD`. Statically linked Go executables do
not load it, although their HTTP traffic can still be replayed.

Save this as `time.go`, build with `CGO_ENABLED=0 go build time.go`, and run the
binary twice with different `FAKETIME` values:

```go
package main

import (
	"fmt"
	"time"
)

func main() {
	fmt.Println(time.Now().UTC().Format(time.RFC3339Nano))
}
```

On an amd64 Linux host with libfaketime installed:

```sh
CGO_ENABLED=0 go build -o time-static time.go

LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1 \
FAKETIME="2020-01-01 00:00:00" \
  ./time-static

LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1 \
FAKETIME="2030-01-01 00:00:00" \
  ./time-static
```

The printed clock remains real. A dynamically linked Python process in the
same container reports the pinned clock.

## Frozen wall-clock deadlines

Record and replay both freeze the container's wall clock at the recorded
epoch so post-startup `time.time()` values match. Monotonic clocks still
advance (`FAKETIME_DONT_FAKE_MONOTONIC=1`), so timeouts built on
`time.monotonic()` keep working, but a loop such as:

```python
deadline = time.time() + 30
while time.time() < deadline:
    ...
```

never terminates because `time.time()` never advances. Reproduce with
`python -c 'import time; time.sleep(0); assert time.time() == time.time()'`
inside a recorded container. Use monotonic deadlines in workloads, and pass
`--timeout SECONDS` to `record`/`replay` so a hung container is killed instead
of blocking forever.

## Replaying with a regenerated mitmproxy CA

Replay pins the container clock to the recording epoch. If the local mitmproxy
CA was generated after that epoch (for example on a new machine), every TLS
handshake inside the container would fail with "certificate is not yet valid".
Replay detects this before starting and exits with an actionable error;
restore the record-time CA from `~/.mitmproxy` or record a new cassette.

## Non-HTTP protocols

Only traffic honored by HTTP(S) proxy variables is captured. UDP, raw TCP,
database wire protocols, Unix sockets, and DNS are outside the cassette.

Reproduce with `python -c 'import socket; socket.create_connection(("example.com",
80)).sendall(b"raw")'`: the direct socket creates no `flows.jsonl` entry.

## Concurrent identical requests

Matching is a FIFO per normalized request key. Sequential identical requests
receive recorded responses in order. If multiple identical requests are
in-flight concurrently, client scheduling can assign response 1 to a
different task than it did during recording.

Runnable origin and workload scripts live in `tests/limitations/`. In one
terminal, start the numbered-response origin:

```sh
uv run python tests/limitations/concurrent_origin.py
```

In another, record and repeatedly replay the two simultaneous identical POSTs:

```sh
mkdir -p /tmp/replayable-concurrency
cp tests/limitations/concurrent_workload.py /tmp/replayable-concurrency/

uv run replayable record \
  --image replayable/agent-base:local \
  --workspace /tmp/replayable-concurrency \
  --out cassettes/concurrent-identical \
  -- python /workspace/concurrent_workload.py

uv run replayable replay \
  --cassette cassettes/concurrent-identical \
  --out-workspace /tmp/replayable-concurrency
```

The response sequence is stable, but which thread prints response 1 versus 2
can swap. Serialize identical requests for the MVP.

## Release status

The automated suite — unit tests, Docker end-to-end tests, and the golden
acceptance replay of the checked-in `research-agent` cassette — passes. What is
not in the repository is `results/`: the 100-run determinism proof and the
latency/cost benchmark are generated from a real recording rather than
committed, so they must be produced locally before being cited. See
[Generate the evaluation results](development.md#generate-the-evaluation-results).
Tag `v0.1.0` once those generated results have been reviewed and committed.

## Other explicit constraints

- Docker containers only.
- No RNG syscall interception.
- Writes outside `/workspace` are discarded with the container.
- Node runtimes may cache `Date.now()` despite `libfaketime`.
- Approximate candidates are diagnostic only and are never served.
- Redaction follows configured headers, secret-like environment names,
  URL-embedded credentials, and optional `[secrets] names` overrides; it is
  not a general data-loss-prevention system.
