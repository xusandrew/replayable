# Replayable documentation

The [project README](../README.md) covers install, quickstart, and a one-line
summary of each capability. Everything below is the detail behind it.

## Using Replayable

| Goal | Guide |
|---|---|
| Install, record, and replay for the first time | [Project README](../README.md#install) |
| Understand a feature, what unlocks it, and how to demo it | [Capability guide](capabilities.md) |
| Look up a CLI flag or exit code | [Command reference](cli.md) |
| Open and operate the local dashboard | [Dashboard guide](dashboard/README.md) |
| Run the complete research-agent demonstration | [Research-agent demo](../demo/research_agent/README.md) |
| Diagnose a CA, TLS, proxy, or mismatch failure | [Troubleshooting](troubleshooting.md) |

## Shipping it

| Goal | Guide |
|---|---|
| Add Replayable to a pull-request workflow | [GitHub Action guide](../actions/github/README.md) |
| Run or troubleshoot the repository workflows | [CI runbook](ci.md) |
| Verify the golden cassette locally | [Acceptance guide](../tests/acceptance/README.md) |

## Working on Replayable

| Goal | Guide |
|---|---|
| Find your way around the modules | [Codebase guide](codebase.md) |
| Run tests, coverage gates, and evaluation scripts | [Development](development.md) |
| Understand the architecture and its tradeoffs | [Architecture decisions](architecture/README.md) |

## Boundaries

| Goal | Guide |
|---|---|
| Know what is and is not protected, and which clients work | [Security model](security.md) |
| See supported boundaries and known limitations | [Limitations](limitations.md) |

The dashboard is packaged inside the Python wheel. End users do not install
Node, pnpm, Vite, or a separate web server.
