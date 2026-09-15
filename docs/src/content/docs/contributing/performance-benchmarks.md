---
title: "Performance Benchmarks"
description: "Run and interpret the Linux x86_64 source-CLI benchmark matrix."
sidebar:
  order: 4
---

The benchmark matrix measures the APM source CLI on Linux x86_64. Use it to
detect command failures and review timing changes.

## Profiles

| Profile | Trigger | Environment | CI result |
| --- | --- | --- | --- |
| PR smoke (`smoke`) | Pull request | Startup plus medium install, update, and compile rows; 5 samples per row | Command, correctness, and harness failures fail. Timing regressions are advisory. |
| Full (`full`) | Schedule or manual run | Startup plus small, medium, and large install, update, and compile rows; 7 samples per row | Command, correctness, and harness failures fail. Timing regressions are advisory. |
| Live GitHub (`live`) | Separate schedule or manual run | One live install row; 3 samples | Observational and non-gating. |

The hermetic profiles use local fixtures and isolated state. They do not use
credentials or network access. The live profile measures external conditions,
such as GitHub service latency. Do not compare live results with the hermetic
baseline.

## Cold and warm samples

Hermetic operation rows use two modes:

- **Cold** removes reusable cache or generated state before the measured command.
- **Warm** uses state prepared by an unmeasured setup command.

Compare cold with cold and warm with warm. Do not combine the modes.

## Harness preparation

For each fixture size in a profile run, the harness builds one protected,
immutable package payload and Git revision A/B template. Each sample receives
reflink-capable copies in sample-local mutable project, repository/origin,
config, cache, temp, and output roots. Shared references never move.

Update setup installs revision A, then replaces the sample-local origin from
the immutable revision B template before `BenchmarkRunner.run_sample`.
`FixtureFactory.prepare_sample` is timed separately from that product command.
The harness writes product timings to `results.json` and preparation throughput
to `preparation.json`, including each sample's `elapsed_ns` and the profile's
`total_elapsed_ns`.

Preparation throughput measures benchmark harness cost. It is not install,
update, compile, or startup latency.

## Run the harness locally

Run the harness from the repository root. The entrypoint is:

```bash
uv run --frozen --no-sync python -m scripts.perf.benchmark_matrix
```

Run and summarize a smoke profile:

```bash
uv run --frozen --no-sync python -m scripts.perf.benchmark_matrix run --profile smoke --output results.json
uv run --frozen --no-sync python -m scripts.perf.benchmark_matrix summarize-results results.json > summary.md
```

`run` writes harness preparation timings to `preparation.json` by default. Use
`--preparation-output PATH` to choose another path. The preparation output must
not be the same path as `--output`.

Download and compare with a trusted baseline:

```bash
uv run --frozen --no-sync python -m scripts.perf.benchmark_matrix download-baseline --repository OWNER/REPO --current-run-id RUN_ID --destination baseline --allow-missing
uv run --frozen --no-sync python -m scripts.perf.benchmark_matrix compare results.json baseline/results.json > comparison.json
uv run --frozen --no-sync python -m scripts.perf.benchmark_matrix summary comparison.json > summary.md
```

Use `run --profile full --output ...` for the complete hermetic matrix. Use
`run --profile live --allow-network --output ...` only when the GitHub
prerequisites are available. Live runs use `summarize-results`; they do not
download or compare a baseline.

`summarize-results` accepts any complete current `smoke`, `full`, or `live`
Linux x86_64 report. Install and update rows use product-default parallelism.

For the deterministic update-ref concurrency proxy, run:

```bash
uv run --extra dev pytest -q -m benchmark tests/benchmarks/test_tiered_resolver_benchmarks.py
```

That focused benchmark compares one-worker and four-worker update-plan ref
annotation with fixed per-ref latency. It asserts bounded overlap and unchanged
underlying unique-ref work. Treat its ratio as an algorithmic guard, not as a
network or end-to-end update measurement.

## Artifacts and baseline

When selected, smoke and full always run after the baseline lookup:

- With `baseline/results.json`, CI runs `compare` and `summary`, then uploads
  `results.json`, `preparation.json`, `comparison.json`, and `summary.md`.
- Without `baseline/results.json`, CI runs `summarize-results results.json`,
  then uploads `results.json`, `preparation.json`, and `summary.md`.

The live job is separate. It uploads `results.json`, `preparation.json`, and
`summary.md`.

| File | Contents |
| --- | --- |
| `results.json` | Profile, environment, scenario definitions, and product timing samples |
| `preparation.json` | Per-sample and total harness preparation timings |
| `comparison.json` | Baseline matches, timing deltas, and comparison statuses |
| `summary.md` | Human-readable result or comparison summary |

Baseline download and comparison use `results.json` only.
`preparation.json` does not affect comparison statuses or baseline
compatibility. The result schema version and comparison semantics are
unchanged.

The baseline downloader accepts only
`benchmark-full-main/results.json` from a successful `main` scheduled or manual
run. It verifies the report revision against the workflow run commit and
requires a complete current authoritative full Linux x86_64 profile. Do not use
a PR smoke artifact or live artifact as the baseline.

With `--allow-missing`, the command exits 0 and leaves no
`baseline/results.json` only when no compatible trusted baseline exists. A
successful full run on `main` becomes the next compatible baseline.

GitHub API or command errors, unsafe or corrupt artifacts, and malformed
reports exit 2. Do not treat these errors as bootstrap.

## Statuses

Comparison rows use `stable`, `improvement`, `regression`, `noisy`, `missing`,
or `incompatible`. The `regression`, `noisy`, and `incompatible` statuses are
advisory and exit 0. A `regression` means that the median crossed both the
percentage and absolute thresholds. Malformed reports and `missing` rows are
hard failures and exit 2.

Review the row, cold or warm mode, absolute change, percentage change, and
baseline commit. Repeat a noisy run. Record a credible regression in the PR,
then fix it or create bounded follow-up work. Do not replace the baseline or
weaken checks to hide a regression.

A command failure, incorrect result, malformed or truncated report, malformed
artifact, or harness error fails the smoke or full job. Fix that failure before
merge.

## Merge-time performance gates

The existing scaling guards remain hard merge-time performance gates. They run
in the normal pytest suite without the `benchmark` marker and fail when growth
crosses their limits. The benchmark matrix does not replace or soften them.

See [Integration Testing](../integration-testing/#performance-selections) for
the selection boundaries between scaling guards, hermetic benchmarks, and live
observations.
