# ADR 0002: Policy resolution

Status: accepted

`core/policy.py` models three modes — `freeze`, `strict-offline`, and
`passthrough`. Resolution is deterministic: CLI override, scenario, scope
rule, channel default. The resolved policy and its hash are pinned in the
cassette manifest.

Old manifests with no policy retain legacy behavior. A replay never reads an
unrelated policy file from the current working directory.

## What is enforced today

Resolution is complete; **enforcement is not**. The replay engine only
implements `freeze`: it serves the recorded flows and reports a mismatch for
anything else. A live segment is requested explicitly with `replay --fork-at`,
which bypasses policy entirely rather than resolving to `passthrough`.

A pinned mode nothing honours is worse than an absent one — the manifest would
describe behaviour the cassette never gets. So `record` refuses to pin any mode
outside `ENFORCED_MODES`, and `replay` refuses a cassette that already pins one.
Both fail before the container starts, with the mode named in the message.

Wiring `strict-offline` and `passthrough` through the replay and fork addons is
the remaining work; `ENFORCED_MODES` is the single place to widen once they are.
