---
title: "Benchmark-Driven Performance Plan"
description: "Plan measured performance work for update, reference resolution, CLI startup, and benchmark throughput."
sidebar:
  order: 5
---

Use this plan to reduce user-visible latency based on current measurements. Use
the [performance benchmark matrix](../performance-benchmarks/) for all
comparison runs.

:::note[Implemented]
Phases 1-5 are complete. This page keeps the technical plan and local results
as historical context. The targets and results remain advisory until they are
compared with a controlled CI baseline.
:::

## Objective

Reduce large-update latency first. Preserve correctness, command contracts,
benchmark guards, and scaling guards.

The implemented first wave executed in this order:

1. Cache the parsed lockfile and ownership indexes.
2. Add a cheaper centralized remote-ref fallback.
3. Load CLI commands only when required.
4. Re-profile integration and optimize only a proven residual hotspot.
5. Reuse benchmark fixture preparation for CI throughput.

Do not combine phases in one change. Separate changes make timing and
correctness regressions easier to isolate.

Phase 6 is implemented. Evaluate each remaining second-wave item in order.

## Measured evidence

These local values come from the large benchmark fixture and targeted profiles.
They set the work order, but they are not release gates.

| Measurement | Result | Meaning |
| --- | ---: | --- |
| `update.cold.large` | 15.119 s | Largest measured user-facing path |
| `update.warm.large` | 14.045 s | Warm state does not remove the main update cost |
| `install.cold.large` | 5.098 s | Secondary user-facing path |
| `install.warm.large` | 4.546 s | Cache reuse provides a limited gain |
| `compile.cold.large` | 1.577 s | Not a primary target |
| `compile.warm.large` | 1.198 s | Scaling is acceptable for this plan |
| `startup.version` | 0.698 s | High fixed cost for a no-work command |

The large update profile produced this additional evidence:

- One large `LockFile.read` takes about 337 ms.
- The run made 19 lockfile reads and 12 `_build_ownership_maps` calls.
- Repeated lockfile reads and ownership-map construction cost about 4.2-5.0 s.
- Reference resolution took about 1.712 s and reported `legacy_clone=12`.
- The trace contained 171 Git executions and 36 clones.
- Integration took about 6.986 s. After the repeated lockfile cost, an
  estimated 2-3 s remains. The evidence does not identify one dominant
  filesystem operation.
- Benchmark fixture preparation took about 266 s, or 39.7% of full-run wall
  time. This time is outside the measured CLI subprocesses.

The traced download phase took about 70 ms. Primitive discovery took about
0.08-0.16 s. The uv launcher took about 25 ms. These areas are not primary
targets.

## Working hypotheses

Treat these statements as hypotheses, not measured evidence:

| ID | Hypothesis | Confidence |
| --- | --- | --- |
| H1 | One parsed lockfile and one pair of ownership indexes will remove most of the measured 4.2-5.0 s repeated cost. | High |
| H2 | `git ls-remote` can resolve most current branch and tag refs before clone-and-introspect on generic or API-failure paths. | Medium |
| H3 | Lazy command imports will remove most of the estimated 0.54-0.62 s APM import and initialization cost from `apm --version`. | High |
| H4 | One or more integration filesystem operations account for a useful part of the remaining 2-3 s. | Low until re-profiled |
| H5 | Reusing immutable fixture inputs can reduce CI wall time without changing measured subprocess latency. | High |

The local URL rewrite fixture forces the GitHub API path to fail. H2 applies to
generic hosts and API failures. It does not describe healthy `github.com` API
behavior.

## Benchmark policy

Apply these rules to every phase:

- Run targeted correctness tests before the benchmark matrix.
- Run the PR smoke profile for each implementation change.
- Run the full profile for phase acceptance.
- Compare cold rows with cold rows and warm rows with warm rows.
- Keep correctness, command, report, and harness failures as hard failures.
- Keep timing status advisory until maintainers approve a controlled CI
  baseline and explicit release thresholds.
- Regenerate the full Linux x86_64 baseline in controlled CI from an unchanged
  `main` revision. Use at least two runs to identify noise. Repeat each row that
  the harness marks `noisy`.
- Reject any credible regression in an unrelated row under the benchmark
  catalog thresholds. Do not replace the baseline to hide a regression.
- Keep `tests/benchmarks/test_scaling_guards.py` in the normal test suite as a
  hard merge-time gate.

## Phase 1: Cache lockfile ownership state

**Status:** Implemented. In a large update profile, `LockFile.read` calls fell
from 6 (7.491 s cumulative under cProfile) to 2 (2.003 s cumulative). The cold
install regression test uses 1 read, and the cold update test uses 2.

**Goal:** Parse the lockfile once per install or update pipeline. Build both
ownership indexes once. Keep the indexes correct during integration.

**Implementation references:**

- `src/apm_cli/integration/skill_integrator.py`,
  `SkillIntegrator._build_ownership_maps`
- `src/apm_cli/install/phases/integrate.py`, the package integration loop
- `src/apm_cli/install/context.py`, `InstallContext.existing_lockfile` and
  run-scoped state
- `.apm/architecture/owners/index.json` and the applicable owner shard

**Work:**

1. Define one canonical owner for the ownership indexes built from
   `InstallContext.existing_lockfile`.
2. Construct the sub-skill-name index and native-skill-path index in one pass.
3. Pass the indexes through the install context or a single integration bundle.
4. Update the indexes after each accepted deployment change. Do not rebuild
   them from disk in each package integrator.
5. Remove direct lockfile reads from integration consumers.
6. Register the canonical owner. Add one behavioral regression guard and one
   static architecture boundary guard.

**Acceptance criteria:**

- A large update performs at most two lockfile reads across the pipeline.
- Ownership indexes are constructed once per integration run.
- Combined lockfile read and ownership-index work falls to 0.4-0.8 s.
- Large update improves by 3.5-4.5 s in both cold and warm modes.
- Cross-package collision warnings, self-overwrite behavior, stale ownership
  cleanup, same-leaf packages from different owners, target scoping, and root
  local-content precedence do not change.

**Tests:**

- Extend `tests/unit/integration/test_skill_integrator.py` for index content and
  incremental updates.
- Add a pipeline regression test that counts lockfile reads and index builds.
- Run ownership lifecycle coverage in
  `tests/integration/test_ownership_invariant_lifecycle.py`.
- Add the static rule and its mutation test to the architecture linter.

**Benchmark gate:** Meet the call-count limits and the 3.5-4.5 s large-update
reduction in a controlled full-profile comparison. Treat any install gain as
secondary evidence because the install profile has not isolated the same cost.

## Phase 2: Add a cheaper current-ref fallback

**Status:** Implemented. In the large local fixture, resolvable refs used
`remote_ref=12` with zero legacy clones. The resolve phase took about 0.990 s
under cProfile, above the provisional 0.45-0.75 s range.

**Goal:** Resolve a current remote ref without a working-tree clone when the
commits API cannot resolve it.

**Implementation references:**

- `src/apm_cli/install/helpers/ref_reuse.py`
- `src/apm_cli/deps/tiered_ref_resolver.py`
- `src/apm_cli/deps/git_reference_resolver.py`

**Work:**

1. Add one centralized remote-ref tier between the commits API and
   `L3LegacyClone`.
2. Reuse the existing authenticated and transport-aware `git ls-remote`
   implementation. Do not add a second Git/auth path.
3. Resolve exact branch and tag refs to a commit. Preserve annotated tag,
   missing-ref, auth, protocol, and host behavior.
4. Coalesce each normalized `(url, ref)` request per run.
5. Keep clone-and-introspect as the final compatibility fallback.

**Acceptance criteria:**

- The large fixture reports zero legacy clones for refs that `ls-remote` can
  resolve.
- The measured current-ref resolution section falls from about 1.712 s to
  0.45-0.75 s.
- Large update improves by another 0.8-1.3 s.
- GitHub, GitLab, Azure DevOps, generic Git, SSH, HTTPS, anonymous-first, and
  token-based behavior remain correct.
- Error text and secret redaction remain unchanged.

**Tests:**

- Extend `tests/unit/deps/test_tiered_ref_resolver.py` for tier order,
  coalescing, misses, ambiguous refs, and clone fallback.
- Extend `tests/unit/deps/test_git_reference_resolver.py` for transport and auth
  reuse.
- Run `tests/integration/test_tiered_resolver_integration.py`.
- Update `tests/benchmarks/test_tiered_resolver_benchmarks.py` to guard the
  unique `(url, ref)` work count.

**Benchmark gate:** Meet the resolution range and clone-count target in the
large local fixture. Also run the live profile as non-gating evidence. Do not
infer healthy `github.com` API gains from the local rewrite fixture.

## Phase 3: Load CLI commands lazily

**Status:** Implemented. In a local 15-sample run, isolated `startup.version`
had a 0.304 s median and a 0.555 s first-process cold outlier. The median is
below the provisional 0.35-0.45 s target.

**Goal:** Do not import command modules that `apm --version` and simple root
errors do not need.

**Implementation reference:** `src/apm_cli/cli.py`, eager command imports and
top-level `cli.add_command` registration.

**Work:**

1. Define one lazy command registry under the top-level Click group.
2. Import a command module only when command lookup requires it.
3. Preserve command names, aliases, help summaries, option parsing, exit codes,
   update checks, and error text.
4. Measure TLS setup after lazy loading. Remove duplicate initialization only
   if the new profile shows a material cost and behavior remains unchanged.

**Acceptance criteria:**

- `startup.version` falls from 0.698 s to 0.35-0.45 s.
- Root `--help`, every command `--help`, the hidden `info` alias, invalid
  commands, and all existing command entry points keep their contracts.
- Frozen-binary command discovery still works.

**Tests:**

- Extend `tests/unit/test_cli_consistency.py` so lazy commands remain
  discoverable and keep explicit help.
- Run `tests/unit/commands/test_helpers_version.py`.
- Run the version and command startup cases in
  `tests/integration/test_core_smoke.py`.
- Add import-boundary tests that prove `--version` does not load unrelated
  command modules.

**Benchmark gate:** Meet the startup range in the smoke and full profiles.
Reject a change that moves import cost into root help or the first command
without documenting and measuring that trade-off.

## Phase 4: Re-profile residual integration

**Status:** Implemented. Reprofiling identified repeated lockfile parsing as
the qualifying residual hotspot, not speculative filesystem work. After the
change, local 3-sample medians were 7.104 s for `update.cold.large` and 3.804 s
for `update.warm.large`. These results remain advisory until they are compared
with a controlled CI baseline.

**Goal:** Select the next integration change from new evidence, not the current
2-3 s estimate.

**Work:**

1. Re-profile `update.cold.large` and `update.warm.large` after phases 1-3.
2. Split remaining integration time by operation, target, and package.
3. Select work only when one operation has a median cost of at least 0.5 s in
   two controlled runs.
4. Prefer fewer filesystem traversals, batched metadata work, and reuse of
   immutable results. Do not add parallel writes without an ownership and
   rollback design.

**Acceptance criteria:**

- The profile names one reproducible hotspot and its share of residual time.
- The selected change reduces that hotspot by 30%-50%.
- The expected large-update gain is 0.5-1.5 s.
- Lifecycle ownership, cleanup, rollback, diagnostics, and output ordering
  remain correct.

**Tests:** Add a regression test at the selected canonical owner. Add or extend
a scaling guard if the change affects complexity. Run the relevant lifecycle
and integration suites.

**Benchmark gate:** Do not start an implementation if the profile does not meet
the 0.5 s selection threshold. Record a no-change result instead.

## Phase 5: Reuse benchmark fixtures

**Status:** Implemented in the P4/P5 benchmark harness work.

**Goal:** Reduce CI preparation time without changing product-latency samples.

**Implementation references:**

- `scripts/perf/benchmark_matrix/fixtures.py`
- `scripts/perf/benchmark_matrix/__main__.py`
- `tests/unit/scripts/perf/test_benchmark_matrix_fixtures.py`

**Implementation:**

1. Build one protected immutable package payload and Git revision A/B template
   per fixture size per profile run.
2. Materialize reflink-capable copies in each sample's mutable project,
   repository/origin, config, cache, temp, and output roots.
3. Install revision A during update setup, then replace only the sample-local
   origin from the immutable revision B template. Shared refs never move.
4. Time `FixtureFactory.prepare_sample` separately from
   `BenchmarkRunner.run_sample`.
5. Keep product timings in `results.json`. Write per-sample `elapsed_ns` and
   `total_elapsed_ns` preparation timings to `preparation.json`.

**Measurement:**

In local full-profile runs, preparation fell from 233.77 s to 181.66 s, a
22.3% reduction. `preparation.json` records these timings separately. They are
advisory evidence, not a release gate.

`RESULT_SCHEMA_VERSION`, baseline comparison semantics, `results.json` product
timings, and measured command boundaries did not change. The workflows upload
`preparation.json` for smoke, full, and live runs.

**Validation:** Fixture tests cover template reuse, copy isolation, cleanup,
cold and warm state, revision A/B setup, and failure recovery. Workflow
contract tests cover preparation artifact uploads.

**Benchmark gate:** Treat this phase as CI-throughput work only. Do not count
fixture preparation gains as install, update, compile, or startup gains.

## Overall acceptance

After phases 1-4, compare the implemented local results with these provisional
large-fixture goals:

| Scenario | Current local median | Provisional target | Implemented local median |
| --- | ---: | ---: | ---: |
| `update.cold.large` | 15.119 s | 8.5-10.5 s | 7.104 s |
| `update.warm.large` | 14.045 s | 7.5-9.5 s | 3.804 s |
| `install.cold.large` | 5.098 s | 4.0-4.5 s | Not measured |
| `install.warm.large` | 4.546 s | 3.6-4.1 s | Not measured |
| `startup.version` | 0.698 s | 0.35-0.45 s | 0.304 s |

The install ranges remain hypotheses until phase 1 provides an install-specific
profile. All targets and local timing comparisons remain advisory until a
controlled CI baseline comparison. Compile must remain stable under the
benchmark catalog thresholds.

## Dependencies and risks

| Item | Control |
| --- | --- |
| Ownership indexes become stale during integration | Use one mutable run-scoped owner and test every ownership transition. |
| A second ownership authority appears | Register the owner and add a static boundary rule. |
| `ls-remote` changes auth or transport behavior | Route through the existing resolver facade and run host-specific tests. |
| Remote ref names are ambiguous | Preserve the legacy fallback when the cheap tier cannot prove one commit. |
| Lazy imports change Click help or PyInstaller collection | Keep a canonical registry and test source and frozen entry points. |
| Filesystem optimization breaks rollback or cleanup | Require lifecycle tests before accepting timing gains. |
| Fixture reuse leaks state into samples | Share immutable inputs only and assert per-sample isolation. |
| CI runners add timing noise | Use controlled full profiles, compatible baselines, and repeat noisy rows. |

Phase 4 depends on phase 1. Phases 2 and 3 can proceed independently, but run
them in the stated order to keep benchmark attribution clear. Phase 5 depends
only on the benchmark harness and must not delay user-facing work.

## Second-wave backlog

Implement these opportunities in order. Keep each change independently
measurable and preserve the freshness, authentication, integrity, and rollback
boundaries established by phases 1-5.

### Phase 6: Order resolver tiers by freshness policy

**Status:** Implemented. Controlled benchmark comparison remains pending.

**Evidence status:** Measured tier call traces show work that the selected
freshness policy cannot use as a terminal answer. The latency gain from
reordering remains a hypothesis until the controlled profile.

**Order:**

- `REPRODUCIBLE` = L0 -> local bare -> commits API -> legacy clone.
- `CURRENT_REMOTE` = L0 -> exact remote -> legacy clone. Omit the commits API
  because exact remote resolution must establish current branch or tag state.

**Impact and risk:** This should remove avoidable network work from warm
reproducible installs and current-remote updates. The main risks are accepting
a stale local ref for `CURRENT_REMOTE`, changing annotated-tag handling, or
altering auth and fallback behavior.

**Acceptance measurements:**

- A warm `REPRODUCIBLE` hit performs zero network requests and zero clones.
- A successful `CURRENT_REMOTE` lookup performs one exact-remote operation per
  unique `(url, ref)`, zero commits API calls, and zero legacy clones.
- Misses preserve the legacy clone result, error, redaction, and host behavior.
- In two controlled large-update runs, median resolver time improves beyond
  benchmark noise and no unrelated benchmark row regresses.

### Ordered follow-on work

| Phase | Opportunity and status | Impact and risk | Acceptance measurements |
| ---: | --- | --- | --- |
| 7 | **Bound concurrent update ref resolution.** **Evidence:** serial independent ref work is measured; wall-time gain remains a hypothesis until a network profile. | High potential on multi-package updates. Bound workers to avoid rate-limit, credential, output-order, and resource regressions. | Assert the maximum in-flight limit, one underlying resolve per unique key, unchanged errors, and a lower median resolve section in two controlled large-update runs. |
| 8 | **Use blobless clones for full-package cache misses.** **Evidence:** cache-miss Git object transfer is measured; host-specific savings remain a hypothesis. | High potential for large repositories. Preserve fallback for hosts that reject partial clone and prevent lazy fetches from crossing auth boundaries. | Compare transferred bytes, cache size, and cold install/update medians. Require identical package contents and successful full-clone fallback. |
| 9 | **Reuse hashes verified in the current run.** **Evidence:** duplicate hashing is measured. | Medium CPU and I/O gain. Reuse only immutable content identities; never carry trust across changed files, sources, or runs. | Hash each eligible content identity once per run, preserve every integrity failure, and reduce hash call count and hash-section time in the profiled fixture. |
| 10 | **Pass materialization Git config through clone-time `-c`.** **Evidence:** five post-clone config subprocesses are measured per affected materialization. | Medium fixed-cost gain. Quoting, config scope, sparse checkout, and promisor behavior must remain identical. | Remove all five post-clone config subprocesses, preserve the resulting Git config, and reduce Git process count and materialization time. |
| 11 | **Reuse thread-local HTTP connections and sessions.** **Evidence:** repeated session and connection setup is measured; end-to-end gain remains a hypothesis. | Medium latency gain for API-heavy runs. Sessions must not cross threads, authorities, explicit credential scopes, or netrc policy boundaries. | Demonstrate same-thread connection reuse, zero cross-thread session sharing, unchanged request count and auth tests, and lower HTTP setup time in two controlled runs. |
| 12 | **Remove eager package-version metadata loading.** **Evidence:** unused metadata reads are measured on paths that do not consume the values. | Low-to-medium fixed and per-package gain. Lazy loading must preserve validation timing, diagnostics, and offline behavior. | Load version metadata only when a consumer requests it, with zero eager loads on the target path and unchanged outputs and failures. |
| 13 | **Index dependency conflicts.** **Evidence:** repeated conflict scans expose worst-case O(n^2) growth. | Medium large-graph gain. The index must preserve conflict precedence, source attribution, and deterministic diagnostics. | Build one run-scoped index, keep lookup work near O(nodes + edges), and keep a 10x-input scaling ratio below 20x with identical conflict results. |

## Non-goals

- Do not optimize the previously traced 70 ms download phase, primitive
  discovery, uv launcher, or compile scaling. This does not prohibit removing
  measured duplicate hashing or reducing Git object transfer on full-package
  cache misses.
- Do not parallelize integration writes before a separate ownership,
  transaction, and rollback design exists.
- Do not weaken correctness checks, scaling guards, report validation, or
  benchmark thresholds.
- Do not use live GitHub results as the hermetic baseline.
- Do not treat fixture preparation as product latency.
- Do not change command behavior, help, errors, output order, auth, or security
  policy to meet a timing target.
- Do not modify `README.md` for this plan.
