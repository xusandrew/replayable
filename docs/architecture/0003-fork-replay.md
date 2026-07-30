# ADR 0003: Fork and hybrid replay

Status: accepted

A fork serves flows before the selected boundary from the immutable cassette,
then permits live upstream traffic. Live secrets are accepted only from an
explicit environment file, validated against the recorded environment shape,
and redacted during capture.

The baseline is never mutated. Replayable builds a temporary composite
candidate and compares transcript, tool sequence, workspace, process exit, and
a deterministic lexical/structural similarity score. `fork-result.json`
contains the review evidence.
