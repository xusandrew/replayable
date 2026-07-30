# Replayable documentation

Start with the guide that matches what you are trying to do:

| Goal | Guide |
|---|---|
| Install, record, replay, fork, and accept a baseline | [Project README](../README.md#research-agent-quickstart) |
| Open and operate the local dashboard | [Dashboard guide](dashboard/README.md) |
| Add Replayable to a pull-request workflow | [GitHub Action guide](../actions/github/README.md) |
| Run or troubleshoot the repository workflows | [CI runbook](ci.md) |
| Understand the architecture and its tradeoffs | [Architecture decisions](architecture/README.md) |
| Run the complete research-agent demonstration | [Research-agent demo](../demo/research_agent/README.md) |
| See supported boundaries and known limitations | [Limitations](limitations.md) |
| Verify the golden cassette locally | [Acceptance guide](../tests/acceptance/README.md) |

The dashboard is packaged inside the Python wheel. End users do not install
Node, pnpm, Vite, or a separate web server.
