# ADR 0002: Policy resolution

Status: accepted

The implemented policy modes are `freeze`, `strict-offline`, and
`passthrough`. Resolution is deterministic: CLI override, scenario, scope
rule, channel default. The resolved policy and its hash are pinned in the
cassette manifest.

Old manifests with no policy retain legacy behavior. A replay never reads an
unrelated policy file from the current working directory.
