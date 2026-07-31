# Security model and supported clients

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



