# resolve-agent

Take a bug that **repro-agent reports as reproduced** to resolution, and prove that
resolution with the reproduction test going fail→pass. It is the step *after*
[repro-agent](../repro-agent/README.md): it consumes that agent's handoff (the
reproduction verdict, the per-facet breakdown, the journey, and the authored e2e
test), then does one of two things:

- **If an open PR already fixes the bug**, it **reviews that PR** — checks out the
  PR, runs the repro test against it, and reviews the full diff for quality and
  scope — instead of writing a competing fix. One concrete failure or requested
  outcome may require changes across layers; independent fixes or features must
  be removed or split out. Polly reports clearly unrelated work as blocking
  and uncertain scope as non-blocking clarification questions in its ordinary
  review. A missing issue link alone is not a scope finding. Resolve addresses
  those findings against the reported bug before approving the existing PR.
- **If no fix exists yet**, it **authors the fix** in a fresh worktree, adds
  targeted tests at the layer it changed, and proves the set goes fail→pass.
  Publication then follows the selected mode: the agent either opens and drives
  the PR itself, prepares a reviewer-facing body for a workflow-owned publisher,
  or stops after a local commit when `skip_push` is enabled.

## Prerequisites

- A configured Claude provider (`omnigent setup` — an Anthropic API key, a
  Claude subscription, an OpenAI-compatible gateway, or a Databricks workspace).
  The agent's brain runs on the Claude Agent SDK.
- `gh` authenticated (`gh auth login`) — the agent finds/reviews an existing fix
  PR, directly published runs open their own PR, and CI recovery reads the run's
  artifacts with it.
- Run it **from the root of your `omnigent-ai/omnigent` checkout** so the agent's
  working directory is this repo.

## Input: a pointer to a completed repro run

Unlike repro-agent (which takes the bug), resolve-agent takes a pointer to a repro
run that already happened — exactly one of:

- **`session`** — the repro-agent session link/id (local: right after
  `dev/repro.py`).
- **`ci_link`** — a CI run URL (when repro-agent ran in throwaway CI and its
  worktree is gone).

From that pointer the agent recovers the verdict/facets/journey and the e2e
test's content. The test **content** can't be pulled from the session transcript
(large tool args are truncated there), so the agent asks the session where it ran
— `sys_session_get_info` returns the repro session's `workspace` (the
`repro/<slug>` worktree) — and reads the full uncommitted test off that worktree's
disk. In CI it pulls the test from the run's artifacts instead. The session id is
the authoritative link back to the right reproduction, so the correct test is
recovered even when several repro worktrees exist.

## Usage

```bash
# From a local repro session (the one dev/repro.py just produced):
omnigent run dev/resolve-agent \
  -p '{"session":"http://localhost:6767/c/dc59e331-..."}'

# From a CI run that executed repro-agent:
omnigent run dev/resolve-agent \
  -p '{"ci_link":"https://github.com/omnigent-ai/omnigent-internal/actions/runs/30974269184"}'
```

### Driver script (isolated worktree)

`dev/resolve.py` wraps the above: it takes the repro pointer (a `session` link/id
or `--ci-link`), creates a fresh **isolated worktree off latest `main`** (branch
`fix/<slug>`, where the slug is derived from the pointer you passed), confirms
with you before launch, then runs the agent from there. It does **not** try to
locate the repro worktree itself — the agent recovers the reproduction (and the
test) from the session, so there's no fragile "which repro worktree?" guess.

```bash
python dev/resolve.py http://localhost:6767/c/dc59e331-...   # local session link
python dev/resolve.py dc59e331-...                           # bare session id
python dev/resolve.py --ci-link https://github.com/omnigent-ai/omnigent-internal/actions/runs/30974269184
python dev/resolve.py <session> --yes                        # skip the pre-launch confirm
python dev/resolve.py <session> --skip-push                  # author mode: commit locally, no push/PR
```

`--skip-push` applies to the author path only: the agent commits the fix in its
local worktree but does **not** push the branch or open a PR, leaving the commit
for you to inspect, push, and PR yourself. It has no effect in review mode, which
follows the existing PR's remediation and publication rules.

### Publication modes

- **Direct publication** is the default when no external publisher contract is
  present. The agent pushes, opens a ready-for-review PR, and drives its preview,
  CI, Polly review, live-validation prompt, and maintainer handoff.
- **Workflow-owned publication** is selected by an explicit CI publisher
  contract with `skip_push` false. The agent commits the fix and prepares and
  validates `.omnigent/pr-body.md` plus the deferred validation prompt, but makes
  no GitHub writes. The workflow preserves the body and handoff in the resolve
  artifact, restores them into the publication worktree, and owns publication
  and recovery.
- **Local-only** is selected by `skip_push` true. It takes precedence over a
  generic publisher overlay: the agent commits locally, prepares no PR body, and
  the workflow suppresses publication.

Because direct publication may **push and open a PR**, and review mode may comment
on an existing PR,
`dev/resolve.py` asks you to confirm before it launches the agent (skip with
`--yes`). The agent itself runs unattended once launched — a direct-mode push is
not gated mid-run, so it works with nobody at a terminal; the ready-for-review PR
is the review gate after the fact.

## What it does

1. Recovers the repro handoff (verdict, facets, journey, `bug_url`) and the e2e
   test's content from the `session` or `ci_link`. CI recovery reads the compact
   artifact checkpoint and preserved test files first, using multi-megabyte job
   logs only as a compatibility fallback for older bundles.
2. **Audits the recovered repro before either authoring or reviewing.** Artifact
   delivery is not validation. Inspect the entire patch for unrelated or unsafe
   changes, check the assertion against the reported behavior, and establish a
   behavioral failure on the exact unfixed base. Preserve the original evidence
   when repairing weak tests; reject unreliable repros rather than changing
   correct product behavior to satisfy them. A green baseline needs an independent
   journey/history check before concluding `nothing_to_fix`; inconclusive or
   unsafe evidence yields `needs_more_info`. Both paths record this in `test_audit`.
3. **Looks for an open PR already fixing the bug.** This decides the path:
   - **Existing fix PR** → checks it out, runs the same audited assertions,
     verifies the journey, reviews the diff for root-cause vs symptom, and
     comments its findings. A green test alone does not prove a fix, and a
     setup/import failure is a verification blocker, not a product regression.
   - **No fix PR** → the author path below, using the same baseline proof.
4. *(author path)* Root-causes, implements the fix, and adds targeted
   unit/integration tests at the layer it changed, each fail→pass on the bug.
5. *(author path)* Re-runs the whole set to prove every live facet goes fail→pass
   (not just a loosened test), and — when the fix touches env-derived defaults —
   **re-runs new tests with ambient vars set** to prove the fixtures are hermetic,
   not flaky-green on a clean machine. When the repro handoff carries
   before-fix recordings, **re-records the same drivers on the fixed tree**
   (building the SPA up front, then pytest-playwright `--video` for web/terminal
   facets, the VHS tape for CLI facets), captions each after clip with the actions
   it performs, and carries the before clips' captions through — so the captioned
   before/after pair lands in the PR's Demo section and, for Linear bugs with a
   key available, on the ticket.
6. *(author path)* Commits the focused, locally validated fix, then follows the
   selected publication mode. Direct runs push and open a **ready-for-review
   PR**. Workflow-owned runs prepare the validated PR body and handoff without
   GitHub writes. Local-only runs stop at the commit. Full repository validation
   and independent review happen after publication.
7. *(direct author path and review path)* **Drives the open PR to a landable
   state** — a bounded loop. Workflow-owned author runs leave this post-publication
   work to the publisher:
   - Labels **every** PR **`ui-preview`** (not just frontend fixes) to request a
     live app deploy — but only **after** CI is green and the Polly review is
     clean for the current commit, never up front, since the label triggers a
     `pull_request_target` deploy of the PR's code. Then waits for the
     preview URL and posts a comment with how to connect a runner to it
     (`omnigent run --server <url>`) to validate the fix directly. (The workflow
     deploys for any labelled non-draft PR, forks included — the label is the
     trust boundary, and only a maintainer-privileged identity can apply it; the
     agent degrades gracefully when no preview appears.)
   - Watches CI (`gh pr checks --watch`); when a check fails it reads the log,
     fixes its own regressions, and pushes — while leaving pre-existing/flaky/infra
     failures alone (and saying so).
   - Reads the latest **Polly AI Review** comment; fixes every actionable finding
     at the root, pushes, and re-triggers `polly-review.yml` for the PR, looping
     until the newest review is clean or the review-round cap is reached.
   - Writes a **paste-to-an-agent live-validation prompt** into the PR body so a
     human can reproduce and confirm the fix, then **tags the issue's assignee**
     (the maintainer) to review once CI is green and the review is clean.
8. Emits a single fenced ```json handoff block: `mode`
   (`reviewed_existing_pr` / `authored_fix`), `outcome` (`fixed` /
   `partially_fixed` / `not_fixed` / `nothing_to_fix` / `needs_more_info`), the
   plain-English `problem_summary` and `solution_summary` used for the Linear
   update, the per-facet fail→pass proof, the PR URL (opened or reviewed, or empty
   until the workflow-owned publisher opens it), and the publication state
   (`ci_status`, `polly_review`, `ui_preview`, `validation_prompt`,
   `maintainer_review`).

It does **not** merge. See `AGENTS.md` for the full operating procedure.
