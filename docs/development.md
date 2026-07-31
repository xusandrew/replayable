# Development and testing

Run the fast unit suite:

```sh
uv run pytest
```

Run lint:

```sh
uv run ruff check .
```

Run all Docker-backed acceptance tests:

```sh
docker build -t replayable/agent-base:local images/agent-base
REPLAYABLE_RUN_E2E=1 uv run pytest tests/e2e -v
```

Run cassette and redaction branch coverage:

```sh
uv run pytest tests/test_cassette.py tests/test_redact.py \
  --cov=replayable.cassette \
  --cov=replayable.redact \
  --cov-branch \
  --cov-report=term \
  --cov-fail-under=90
```

Run matcher branch coverage:

```sh
uv run pytest tests/test_matcher.py \
  --cov=replayable.matcher \
  --cov-branch \
  --cov-report=term \
  --cov-fail-under=90
```

The GitHub Actions workflow runs:

1. locked dependency installation;
2. Ruff;
3. unit tests;
4. cassette/redaction coverage;
5. matcher coverage;
6. mitmproxy CA generation;
7. demo image build;
8. all Docker E2E tests.




## Generate the evaluation results

After recording and successfully replaying the research agent, run the
100-replay proof:

```sh
uv run python scripts/prove_determinism.py \
  --cassette cassettes/research-agent \
  --runs 100
```

This writes `results/determinism.json` and fails if either workspace or stdout
has more than one observed hash. Then generate the latency/cost comparison:

```sh
uv run python scripts/benchmark.py \
  --cassette cassettes/research-agent
```

This writes `results/benchmark.json` and `results/benchmark.md`. The built-in
Claude Haiku 4.5 rates are $1/input MTok and $5/output MTok, verified against
Anthropic's published pricing on 2026-07-18. For another model, pass
`--input-price-per-million` and `--output-price-per-million`.

These result files are intentionally not fabricated in the repository: they
must be generated from the real cassette used in the evaluation.

