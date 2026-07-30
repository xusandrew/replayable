# ADR 0001: Versioned event log

Status: accepted

Replayable stores ordered, typed events beside network flows. Existing v1
cassettes are projected into the event model at read time, so introducing the
timeline did not require rewriting or orphaning recordings. New recordings
dual-write a network event for every flow and validate the one-to-one mapping
before completion.

The event model is the stable UI and verdict read surface; `flows.jsonl`
remains the network replay source of truth.
