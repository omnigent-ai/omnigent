---
name: resolve-impact-assessment
description: Check the full candidate diff, affected consumers, focused tests, evidence identities, and verification gaps.
---

## Shared impact assessment — every resolution path

Before claiming `fixed` or approving a PR, check what the **full final diff**
could break beyond the reported bug. This applies to author, existing-PR review,
ticket-only, and review-remediation modes, including `skip_push` and
workflow-owned publication. Ticket-only and review-remediation still do not
require an inherited repro; their publication/branch permissions are unchanged.

1. **Inspect the candidate and its consumers.** Start the assessment while
   investigating, then refresh it after the last edit. Record the target branch's
   full tip SHA as `base_sha` and the candidate's full HEAD SHA as `head_sha`.
   Inspect `git diff <base_sha>...<head_sha>` plus staged, unstaged, and untracked
   source/test changes. Include configuration, dependencies, and workflows;
   runtime prompts count as behavior changes even when stored in Markdown. In
   review modes, inspect the whole PR, not only edits made in this run. Follow
   shared helpers to their callers and existing tests. For each meaningful risk,
   identify the changed files, affected behavior, consumers, invariant to preserve,
   and focused check. A passing original repro is not coverage for every consumer.
2. **Exercise the relevant boundaries.** Select checks from the actual change:

   | Changed behavior | Required focused coverage when applicable |
   | --- | --- |
   | Shared startup, configuration, auth, or serialization | Existing/default configurations and other affected consumers; exercise configuration creation, transport/decoding, and real process startup together. Correctly constructed objects supplied directly to a unit test do not cover that path. |
   | Sandboxing | The applicable real OS sandbox with its intended gates enabled. A disabled gate or an unsupported/skipped sandbox test leaves that boundary uncovered. |
   | UI state or interaction | The reported journey and affected adjacent journeys on the candidate build, including relevant selection, persistence, or reconnect behavior. |
   | Environment-derived defaults | Ordinary configuration and relevant ambient variables populated; apply the hermetic check in 2B.5. |
   | Performance or timing | Measure the claimed quantity under the relevant workload. Distinguish simulated-clock tests from elapsed-time measurements; functional success alone does not establish latency or throughput. |

   Use existing tests where they cover the invariant; add a behavioral regression
   test only for a meaningful gap. Tests of the bug follow their mode's baseline
   requirements; review-remediation still skips fail-before proof. Checks
   protecting previously correct behavior may **pass on both
   base and candidate**; do not manufacture a failing baseline for them. Run the
   focused checks for each affected consumer/boundary, even when they live in a
   different module. Do not run the full repository suite locally. Do not weaken
   isolation or change global rollout settings to obtain a passing result.
3. **Record observed results in `impact_assessment`.** Each risk names its check,
   `result` (`passed`, `failed`, `blocked`, or `not_run`), and retained evidence.
   Reference actual command/output or CI run artifacts; include the tested
   revision/build, dependency pins, and relevant environment/feature gates.
   Confirm the test imports and exercises this candidate, not another checkout,
   installed copy, stale SPA, or older CI runtime. Record `worktree_state` as
   `clean` only when the tested source/tests are clean; otherwise name the
   uncommitted source/test/support files and their content hashes. Artifact-only
   dirt can be noted separately. A HEAD SHA alone does not identify dirty code.

   Distinguish component tests, real process/sandbox checks, and live bot checks.
   An unrun, skipped, xfailed, or setup-failing required check is not a pass:
   name the missing boundary and reason in `uncovered_boundaries`. A large green
   test count or an unrelated green CI job does not fill that gap. Evidence
   references describe what was retained; never invent an execution record.
4. **Refresh before delivery.** Re-read the final diff, Git status, and current
   base/head before every final handoff or review verdict. For an existing PR,
   compare with the live GitHub base/head SHAs, not just local refs. If the author
   moved the head, follow the mode's update/retry procedure without approving
   from old results. After a retry, new PR head, merge/rebase, CI/Polly fix, or
   changed code, assertions, dependencies, or relevant environment, rebuild the
   assessment and rerun affected checks. Reuse
   a result only when its tested contents and context still match; retain its
   original identity rather than relabeling old evidence as a new execution.
   Commit hooks can change tested files too. Keep earlier evidence and the saved
   checkpoint; never reset work to make the identities match.
5. **Let gaps affect the verdict.** `fixed` requires the original behavior to be
   proven as its mode requires, all identified regression risks checked, and no
   uncovered required boundary. A discovered regression must be repaired and
   checked before `fixed` or approval. If a fix is only partly verified, preserve
   the work, use `partially_fixed`, and name the failing/unrun checks in
   `remaining_work`; use `needs_more_info` when verification cannot establish a
   resolution. Setup failures are blockers, not proof of a product regression.
   A missing recording alone still follows the recording rules below.

Summarize the affected behavior, checks, and gaps in the PR's Test Plan or review
body. The assessment is an agent-authored explanation, not an execution recorder
or a guarantee of no regressions. Record empty `risks` only with a concrete
`not_applicable_reason` (for example, no candidate exists yet, or inspection found
only documentation that is never loaded at runtime); never use it to excuse an
untested behavior change.
