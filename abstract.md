# Abstract

AI agents are nondeterministic: the same input can produce a different output on
every run. This breaks the assumption all software testing rests on — same
input, same output — and with it the most basic engineering question you can ask
about a system: *I changed something, did I break it?* Re-running an agent
changes its behaviour anyway, so a difference between two runs is
indistinguishable from the model rolling different dice. Production failures are
non-reproducible by construction, and every test run costs money and minutes,
which puts an agent test suite on every commit out of reach.

**Replayable** is a testing harness that makes agents reproducible so they can be
tested like ordinary software. Nondeterminism enters an agent through a small
number of channels; intercept those and a run can be recorded once and replayed
exactly. Replayable sits a TLS-intercepting proxy at the container boundary to
capture every model call and tool call, pins the clock with `libfaketime`, and
runs the agent in a fresh container from a pinned image with a content-hashed
workspace snapshot. Because the interception happens at the boundary rather than
inside the agent's libraries, **the agent runs unmodified** — no wrapper, no SDK,
no declared tool list.

The design principle is *selective determinism*: freezing everything would test
nothing, so the developer chooses which channels stay frozen and which are
allowed to vary. Freeze the model and tools while varying the code to ask "did my
change break this?"; freeze the code and resample the model to ask "is this
failure real or noise?"; freeze everything but one tool and inject a failure to
ask "how does this behave when a dependency dies?"

The current implementation records an unmodified Python agent's HTTP, clock and
filesystem channels and replays it offline and byte-identically. A recorded
20-call research agent replays in 0.62 s for $0.00 against 32 s and real API cost
to record, reproducing its workspace and transcript hashes exactly.

The remaining work turns that mechanism into a product: a policy engine for
selective determinism, a verdict engine that distinguishes a real regression from
a flaky test statistically rather than heuristically, a run explorer for
inspecting and forking recorded runs, and CI integration that replays golden runs
on every pull request and gates merges on the result. The claim to be evaluated
is not that runs replay — it is that the harness catches genuine behavioural
regressions without reporting noise as one, measured as precision and recall over
a corpus of seeded regressions with known ground truth.
