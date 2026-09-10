# Flaky-test audit: latest 1,000 PRs

## Outcome

**Keep coverage; fix demonstrated test defects, not every red test.** The audit
found 237 pytest nodes with intermittent execution evidence, including 89 under
`tests/e2e/` or `tests/e2e_ui/`. Intermittency does not establish that a test is
wrong: product races, shared setup, mutable dependencies, and infrastructure can
also change the result at a fixed checkout.

This PR stabilizes two short browser interactions and two mock-host fixtures,
adds two idle-host regressions, and removes **zero tests**. No skips, xfails,
parametrizations, or retry settings change. Its four test files do not overlap
the separate, now-merged [#6978](https://github.com/omnigent-ai/omnigent/pull/6978).
The fixes were developed and tested on base `93fb7e7a1`.

## Scope and availability

The cohort is the latest **1,000 created PRs, all states**, selected at
**2026-09-10 20:47:29 UTC**: #5718 through #6983, with non-contiguous numbers.
The oldest was created on August 28 at 08:51 UTC. Workflow discovery covers
August 28 06:51 UTC through the cohort cutoff; metadata states were recorded
during collection, not reconstructed at an exact historical instant.

| Measure | Result |
| --- | ---: |
| PRs enumerated | 1,000 |
| PRs with associated test workflows and job logs | 994 |
| Matched test workflow runs / enumerated attempts | 17,277 / 17,603 |
| Completed attempts when metadata was collected | 17,592 |
| Downloaded archives / HTTP 404 archives | 17,551 / 41 |
| Downloaded archives with no top-level job logs | 1,057 |
| Attempts not completed when metadata was collected | 11 |
| Parsed job logs | 85,605 |
| Pytest nodes with a failure or retry signal, excluding the intentional retry self-test | 5,266 |
| Nodes with recovered retries / mixed outcomes at identical checkout | 71 / 181 |
| Union of those two evidence sets | 237 |

- [prs.csv](prs.csv) enumerates every cohort PR and its coverage. No matching test
  workflow was found for #6917, #6846, #6437, #6291, #6112, or #5780; these are
  **not clean-test results**.
- [unavailable.csv](unavailable.csv) records all 41 HTTP 404 attempts, associated
  with #6238, #6231, #6188, and #5885. The API response does not establish why
  those logs are unavailable. Rate-limit errors were waited out, not counted as
  permanent gaps. Collection finished on September 10 at approximately 23:11 UTC.
- [tests.csv](tests.csv) lists all 237 intermittent pytest nodes, their counts,
  representative run/attempt URLs, job names, checkout evidence, and decisions.
- [web-failures.csv](web-failures.csv) lists 137 Vitest failure nodes separately.
  Passing logs provide file-level, not individual-test, results; these failures
  are **not classified as flakes** from that evidence.

The original 21,014 matched runs include non-comparable workflows excluded from
test evidence: Code Coverage (1,055), Nightly Failure Monitor (1,479), Nightly
Release (4), UI Snapshot Failure Comment (729), and UI Snapshot Update (470).
The first four publish, monitor, or orchestrate results; the last deliberately
rewrites expected baselines and is not fixed-code evidence.

## Evidence rules

1. Enumerate PRs and commits with pagination. Discover test workflows and split
   time windows below GitHub's run-search cap rather than truncating at 1,000 runs.
2. Associate runs by explicit PR reference first (2,916 runs), commit SHA next
   (10,090), then matching branch **and repository ID within the PR's open window**
   (4,271). A shared commit can associate a run with multiple PRs.
3. Read available completed-attempt archives, including successful runs: a green
   job may contain recovered retries. Reuse immutable cached archives, not stale
   aggregate counts from the earlier 200-PR audit.
4. Count an intermittency signal only for **same-job `RERUN` then `PASSED`**, or
   failure and pass with the **same logged checkout SHA, workflow, and normalized
   job name**. A matching branch, PR head, or run ID alone is insufficient.
5. Never turn missing, skipped, canceled, unfinished, or no-result tests into
   passes. Exclude `test_llm_flaky_rotates_model_per_attempt`, which deliberately
   exercises the retry mechanism. Pytest is the primary per-test parser; other
   frameworks are not assigned invented per-test pass counts.

Counts are **test/job/attempt observations**, not independent probabilities or a
current flake rate. Historical revisions, shards, fixture errors, and retries
are pooled. The parser retains one final outcome per pytest node within each job
log; it recorded 40,218,897 passes, 12,307 failures, 16,036 errors, and 36 timeouts.
Setup breakages can produce thousands of failing nodes without thousands of
defective tests. The 71 and 181 evidence sets overlap in 15 nodes.

In `tests.csv`, a retry-only row links the recovering job; a same-checkout row
links a comparable failure/pass pair. Job names use archive filename escaping
(`/` becomes `_`). Open the linked attempt, find the job, and search its log for
the full pytest node. Job-level conclusions are not inferred from absent job IDs.

## Changes supported by reproduction

| Existing test | Recovered observations / pass-fail observations | Representative same-job recovery | Change |
| --- | ---: | --- | --- |
| `test_agent_info_opens_on_hover_and_bridges_to_panel[chromium]` | 25 / 1,897 | [CI log](https://github.com/omnigent-ai/omnigent/actions/runs/33358895256/job/99386549405) | Control browser time during gap/panel dwell; await real animations; require `data-state="open"` as well as visibility. |
| `test_sidebar_click_cancels_pending_peek[chromium]` | 8 / 1,834 | [CI log](https://github.com/omnigent-ai/omnigent/actions/runs/33689078080/job/100446187362) | Control the hover/press/release intervals while retaining real pointer events and peek/pinned-state assertions. |
| `test_list_worktrees_returns_data` | 8 / 1,532 | [CI log](https://github.com/omnigent-ai/omnigent/actions/runs/33939378242/job/101233706225) | Keep the mock-host drain alive during idle periods; explicitly cancel/await it and await disconnect on teardown. |
| `test_create_directory_returns_created_path` | 1 / 1,571 | [CI log](https://github.com/omnigent-ai/omnigent/actions/runs/34425312740/job/102709213170) | Apply the same fixture lifetime correction and add an idle regression. |

The host fixtures used `ApplicationCommunicator.receive_output(timeout=0.5)`.
An asgiref receive timeout cancels the underlying ASGI application; catching
`TimeoutError` does not resurrect it. The replacement waits without an idle
deadline and shuts down explicitly. The new regressions intentionally idle for
750 ms before verifying the connection and a successful request; that sleep is
the scenario input, not a synchronization workaround.

For the browser tests, driver latency could turn an intended short gesture into
a different, longer gesture. The clock is installed before navigation and paused
before interaction. CSS animation completion, actual pointer events, and all
behavioral assertions remain. Checking only popover visibility was insufficient:
a closing animation could still be visible and permit re-entry to conceal a
broken bridge. The added open-state assertion rejects that case.

These reproductions establish test defects; they do **not** attribute every
historical retry to them. The terminal failures for these four nodes include
server-startup and migration failures, which this PR does not claim to fix.

## Retained cases and follow-up priorities

| Historical case | Recovered observations | Decision |
| --- | ---: | --- |
| Concurrent credential writes / hover timestamps | 269 / 86 | Separate #6978 addresses synchronization and fixture lifetimes; no duplicate changes here. |
| Cursor model-listing row / mobile queued-row targets | 162 / 54 | Completed-turn expansion and hidden-terminal pointer interception were addressed in [#6493](https://github.com/omnigent-ai/omnigent/pull/6493). Retain. |
| Sharing journey / jump-to-top auto-hide | 127 / 41 | Existing stabilization in [#6375](https://github.com/omnigent-ai/omnigent/pull/6375). Retain. |
| Composer/transcript growth | 40 | Existing stabilization in [#6305](https://github.com/omnigent-ai/omnigent/pull/6305), with later layout changes in [#6522](https://github.com/omnigent-ai/omnigent/pull/6522). Retain. |
| Codex working indicator across restart | 63 | Product restart fix [#6957](https://github.com/omnigent-ai/omnigent/pull/6957) already exists. Do not delete its regression guard. |
| Optimistic first-prompt title | 73 | Failures include a missing initial `/events` POST. Investigate the handoff rather than reducing the test to a title-only assertion. |
| Previous-session model label / initial-prompt session switch | 57 / 40 | Valuable isolation/routing guards. Product/render races versus observation timing remain unresolved. |
| REPL reasoning-effort dispatch / concurrent usage accumulation | No recovered retries; 29 / 15 same-checkout mixed-outcome groups | Unexpected generic mock responses and agent-cache/config parsing failures need root-cause work. Do not remove the dispatch checks or serialize concurrent writers. |

The retained-case references identify relevant prior fixes, **not certification
that every historical failure is resolved**. All other intermittent nodes remain
listed in `tests.csv`; absent or renamed historical nodes are identified rather
than treated as deletions in this PR. Failure-only candidates without the evidence
above are not labeled flaky. No broad assertion weakening or quarantine is used.

## Validation and how to test

All local runs use mock providers and `--force-reruns=0`, overriding existing
retry markers. Results below are for the final test changes.

| Check | Result |
| --- | --- |
| Two new idle-host regressions, 50 repetitions each | 100 passed |
| Two browser timing tests, 50 repetitions each with 350 ms driver delay | 100 passed |
| All four changed modules | 11 backend + 7 UI tests passed |
| Sharing, jump-to-top, composer-growth, model-listing, and mobile-target modules retained from main | 9 tests passed |
| Original idle fixtures against the new regressions | Both regressions failed; original teardown also produced cancellation/setup errors |
| Original browser tests with the same driver delay | Both failed |
| Negative controls: immediate popover close; disabled pointer-down cancellation | Both revised tests failed on the intended open/peek assertions |
| AST comparison against base | All 16 existing tests and decorators retained; 2 tests added |
| Pre-commit on changed files | Passed |

The temporary stress helper parametrized each target 50 times, delayed Python
`Mouse.move` return and `Mouse.down` entry by 350 ms, and separately injected the
two browser faults. It is not part of the product or committed test harness.
Passing repetitions are evidence for these fixes, not a guarantee of zero future
flakes. Regular functional coverage is reproducible from this checkout:

```bash
uv sync --locked --extra all --group dev
pnpm install --frozen-lockfile
pnpm --dir web build
env -u DATABRICKS_TOKEN -u DATABRICKS_CODEX_TOKEN \
  -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  OMNIGENT_WRAPPER_BYPASS=1 OMNIGENT_CONFIG_HOME="$(mktemp -d)" \
  uv run --no-sync pytest \
  tests/server/integration/test_hosts_create_directory.py \
  tests/server/integration/test_hosts_worktrees.py \
  tests/e2e_ui/sessions/test_agent_info_hover.py \
  tests/e2e_ui/sessions/test_sidebar_toggle_hotkeys.py \
  --ui-skip-build --force-reruns=0 --timeout=90 --tb=short -q
```

Expect **18 passed**. Review the diff to confirm that the bridge/peek assertions
remain, the two host tests actually send requests after idling, and no existing
test or parameter case was removed. This is test-only work; no visual demo or
production configuration change is required.
