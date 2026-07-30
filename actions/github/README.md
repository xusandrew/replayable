# Replayable GitHub Action

The composite action runs a replay, writes the verdict to the job summary,
creates or updates one pull-request comment, emits JUnit XML, uploads diagnostic
artifacts, and fails with Replayable's exit-code contract.

The caller must check out the repository, install its locked Python
environment, generate a replay CA, and build the workload image first.

```yaml
permissions:
  contents: read
  pull-requests: write

steps:
  - uses: actions/checkout@v6

  - uses: astral-sh/setup-uv@v7
    with:
      python-version: "3.12"

  - run: uv sync --locked
  - run: uv run python scripts/make_replay_ca.py --not-before-days 3650 --force
  - run: |
      docker build -t replayable/agent-base:local images/agent-base
      docker build -t replayable/research-agent:local demo/research_agent

  - uses: ./actions/github
    with:
      cassette: tests/fixtures/cassettes/research-agent
      strict: "true"
      allow-image-mismatch: "true"
      stale-after-days: "30"
      cache-key: ${{ hashFiles('demo/research_agent/**') }}
      github-token: ${{ github.token }}
```

## Inputs

| Input | Default | Meaning |
|---|---:|---|
| `cassette` | required | Cassette directory |
| `strict` | `true` | Fail on unconsumed recorded flows |
| `allow-image-mismatch` | `true` | Use the recorded tag after rebuilding the image |
| `stale-after-days` | `30` | Non-failing baseline-age warning |
| `fork-at` | empty | Optional live fork boundary |
| `env-file` | empty | Secrets for a live fork |
| `cache-key` | empty | Optional agent-source-keyed cassette cache |
| `github-token` | empty | Enables PR comment creation |
| `comment` | `true` | Disable for schedules and non-PR runs |

The action outputs `exit-code`: `0` success, `1` agent failure, `2` behavioral
mismatch, or `3` harness/configuration failure.

Never run a secret-backed fork from an untrusted pull request. The repository's
live drift workflow runs only on a schedule or explicit dispatch.
