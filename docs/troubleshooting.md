# Troubleshooting

## `mitmproxy CA not found`

Run:

```sh
uv run mitmdump
```

Wait for startup, stop it with Ctrl-C, and verify:

```sh
test -f ~/.mitmproxy/mitmproxy-ca-cert.pem
```



## TLS certificate errors inside the workload

Confirm the client honors the injected CA variable. Curl specifically uses
`CURL_CA_BUNDLE`; Requests uses `REQUESTS_CA_BUNDLE`; many OpenSSL clients use
`SSL_CERT_FILE`.

Certificate-pinning clients cannot be transparently intercepted.

## No flows were recorded

Check:

1. the workload actually used HTTP or HTTPS;
2. its client honored `HTTP_PROXY` or `HTTPS_PROXY`;
3. it did not bypass the proxy through a custom transport;
4. `proxy.log` contains the expected host.

Curl intentionally ignores uppercase `HTTP_PROXY` for plain HTTP URLs. The
included demo uses HTTPS. For a plain-HTTP curl debugging workload, pass
`--proxy "$HTTPS_PROXY"` explicitly.

## Proxy port 8080 is already in use

The CLI defaults to port 8080. Pass `--port 0` to pick a free ephemeral port
(or `--port N` for a specific one), or stop the conflicting process.

## Docker exits 125

Replayable treats Docker exit 125 as a harness failure. Verify:

- Docker Desktop or Docker Engine is running;
- the image exists locally;
- the image name is correct;
- mounted paths are shared with Docker Desktop.



## Replay exits 2

Inspect:

```sh
python -m json.tool ./cassettes/your-cassette/replay-report.json
python -m json.tool ./cassettes/your-cassette/replay-state.json
```

Use `inspect --explain-match` to compare canonical requests. Do not solve a real
prompt or control-flow change by indiscriminately marking fields volatile.

## Normalization rules do not match the manifest

The cassette's `replayable.toml` changed or is missing. Restore the exact file
used during recording or record a new cassette. Replay intentionally refuses
to use a different ruleset silently.

## Replay exits 1 without a mismatch report

The container command itself failed before making a mismatched HTTP request.
Run the image and command manually, then inspect `replay-proxy.log`.

