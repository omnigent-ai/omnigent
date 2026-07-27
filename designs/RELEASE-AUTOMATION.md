# Deterministic release pipeline

Status: accepted 2026-07-14; implemented in this repo 2026-07-15 (release.yml,
finalize-release.yml, update-homebrew.yml, bump-version App token, branch-CI
triggers, lockstep CI check, RELEASING.md rewrite). Secure-repo restructure and
the tag ruleset are follow-ups. Owner: @dhruv0811.

Today a release is an LLM agent (or human) walking `RELEASING.md` step by step:
~15 CLI commands across two GitHub accounts, two repos, a hand-edited lockfile,
and judgment calls interleaved with mechanical steps. Every step of that runbook
is either already a workflow or trivially expressible as one. This doc proposes
collapsing the mechanical 90% into **two `workflow_dispatch` runs per release
phase** (rc, then final), parameterized by `version` + `ref`, while keeping every
human-judgment point (publish approval, notes curation, docs review) as an
explicit gate rather than an implicit runbook step.

## What exists today (verified against the repo, 2026-07-14)

The pipeline is already more automated than RELEASING.md's manual framing
suggests. Per release step:

| Step | Mechanism today | Deterministic? |
| --- | --- | --- |
| Cut `release/vX.Y.0` from green main/SHA | human CLI | ❌ manual |
| Lockstep bump (3 `pyproject.toml` + `omnigent/version.py` + `uv.lock`) | `scripts/update_versions.py` (+ `bump-version.yml` wrapper) | ✅ exists, but human-invoked; RELEASING.md still says "hand-edit `uv.lock`" (CI `uv lock` has no proxy problem) |
| Tag `vX.Y.Z[rcN]` + push | human CLI | ❌ manual |
| Bump main to next `.dev0` | human CLI (or `bump-version.yml` post-release) | 🟡 semi |
| Draft GH release (prerelease flag for rc, rerun-safe) | `github-release.yml` on tag push | ✅ |
| CHANGELOG PR + LLM-curated draft notes | `draft-release-notes.yml` via `workflow_run` (final tags only) | ✅ |
| Secure-repo gates + PyPI publish | manual `gh workflow run omnigent.yml` ×2–3 (dry-run, [test-pypi], pypi) in `databricks/secure-public-registry-releases-eng` | ❌ manual dispatches |
| Post-publish validation (clean venv install + `--version`) | human CLI recipe | ❌ manual |
| Publish GH release as Latest | human UI click | ❌ manual (and API publish does **not** set `make_latest` unless told to) |
| Site release post + `X.Y-docs → main` PR | `publish-changelog.yml` on `release: published` | ✅ |
| Sweep open doc PRs against `X.Y-docs` before docs go live | nobody | ❌ missing |
| Docker images (`:vX.Y.Z`, `:latest`, `:latest-rc`) | `oss-publish-images.yml` on tag push, PEP 440-ordered moving tags | ✅ |
| Homebrew formula bump (`omnigent-ai/homebrew-tap`) | nobody — tap frozen at **0.2.0** while PyPI is at 0.5.1 | ❌ missing |

Internal precedent: the VS Code extension track already ships the exact target
shape — `vscode-release-pr.yml` (`version`, `dry_run` → bump PR) +
`vscode-extension-release.yml` (`version`, `dry_run` → build + draft release).
This proposal is the same pattern applied to the Python release.

Actual release history confirms the rc-then-final model this automates:
`v0.4.0rc1 → rc2 → v0.4.0`, `v0.5.0rc1 → rc2 → v0.5.0 → v0.5.1` (patch), with rc
GitHub releases left as prerelease drafts.

## Target model

Per phase (rc or final), the human does:

```
rc:    dispatch release.yml (version=0.6.0rc1)      # cut/bump/tag — one run
       dispatch secure omnigent.yml (ref=v0.6.0rc1) # gates → [approve] → publish → validate
final: dispatch release.yml (version=0.6.0)
       dispatch secure omnigent.yml (ref=v0.6.0)
       …curate the draft notes, merge the CHANGELOG PR…
       dispatch finalize-release.yml (tag=v0.6.0)   # checks → [approve] → publish-as-Latest
       …merge the two site PRs it triggers…
       …review the auto-opened homebrew-tap bump PR, apply the pr-pull label…
```

Two runs per phase (finalize is the third, final-only, and exists to *gate*
judgment, not do work). Everything inside a run is deterministic, idempotent,
and re-dispatchable after a failure with the same inputs.

Deliberately **not** one run: the secure-repo dispatch stays separate because it
crosses the org/account boundary that repo exists to enforce. Auto-dispatching
it from the public repo would require storing a Databricks-account PAT in
`omnigent-ai/omnigent` — weakening the isolation for the sake of one saved
click. Rejected.

## Workflow 1 — `release.yml` (new, omnigent-ai/omnigent)

`workflow_dispatch` inputs:

- `version` — `0.6.0rc1` | `0.6.0` | `0.6.1` (no leading `v`; `.dev` rejected)
- `ref` — default `main`; branch/tag/SHA to cut from. **Only consulted when
  `release/vX.Y.0` does not exist yet** (i.e. at rc1). Later rcs, the final, and
  patches always build from the existing `release/vX.Y.0` head; passing a `ref` that
  disagrees with it fails loudly instead of silently retargeting.
- `dry_run` — default `true` (repo convention, matches the vscode workflows):
  run the whole plan, print it, push nothing.

Jobs:

1. **plan** (always): validate version shape (reuse `bump-version.yml`'s PEP 440
   regex minus `.dev`); derive `release/vX.Y.0` + `vX.Y.Z[rcN]`; resolve the base SHA
   (existing branch head, else `ref`); assert the tag doesn't exist (or already
   points at the fully-converged state → declare no-op); assert the resolved
   SHA's check suites are green (not just "some run on main succeeded"); for a
   final, warn if no `vX.Y.*rc*` tag exists on the branch. Write the plan to the
   step summary.
2. **execute** (`dry_run == false`): mint the omnigent-ci App token; create
   `release/vX.Y.0` at the base SHA if missing; `update_versions.py pre-release
   --new-version $VERSION`; `uv lock` (runner resolves against real PyPI — this
   *retires the hand-edit-uv.lock ritual entirely*); `update_versions.py check`;
   commit `release: vX.Y.Z` (skip when already stamped); tag; push branch + tag
   **with the App token**. Pushing with the App token (not `GITHUB_TOKEN`) is
   load-bearing: `GITHUB_TOKEN`-pushed tags do not trigger workflows, and the
   whole downstream chain (`github-release.yml` → `draft-release-notes.yml`,
   `oss-publish-images.yml`) hangs off that tag push.
3. **bump-main** (only when the branch was created in this run, i.e. rc1):
   `gh workflow run bump-version.yml -f mode=post-release …` — opens the
   `main → next .dev0` PR immediately at branch cut, exactly as RELEASING.md
   step 1 prescribes ("keep main from re-freezing"). Merging it promptly also
   matters for docs: `doc-sync.yml` derives the `X.Y-docs` staging branch from
   main's version. **Decided:** `bump-version.yml` switches its PR-creation
   push to the omnigent-ci App token (falling back to `GITHUB_TOKEN` where the
   App vars are absent, e.g. forks) so CI runs on bump PRs — retiring the
   documented "push an empty commit to kick CI" workaround.
4. **summary**: print the exact secure-repo dispatch command for this tag.

Idempotency contract: branch exists → reuse; version already stamped → no
commit; tag exists at the converged commit → no-op; tag exists elsewhere →
fail. A half-failed run is always safe to re-dispatch verbatim.

Security posture: this executes repo scripts from a maintainer-chosen,
CI-green commit under `workflow_dispatch` — the same trust level as the
existing `bump-version.yml`. The no-code-exec guarantee of `github-release.yml`
(which is *tag-triggered*, attacker-influenceable) is unaffected.

## Workflow 2 — secure repo `omnigent.yml` restructure

Today: 2–3 dispatches (dry-run=true, optional test-pypi, then pypi) with manual
validation between. Proposal — same file, split into three chained jobs so one
dispatch covers the user flow "dry-run, then real publish, then validate":

1. **gates** (always): build all three distributions once; dependency scan;
   lockstep/pin verification; web-UI-in-wheel; `twine check`; smoke-install.
   Upload the built artifacts as run artifacts. This *is* the dry run.
2. **publish**: `needs: gates`, bound to the protected Trusted-Publisher
   environments (required reviewer = the human authorization click). Downloads
   the **same artifacts** — never rebuilds, so what was scanned is what ships.
   Before each upload, probe `https://pypi.org/pypi/<pkg>/<ver>/json` and skip
   already-published packages (`skip-existing` semantics): a partially-failed
   publish is healed by re-running instead of yanking, because the remaining
   identical artifacts complete the set.
3. **validate**: `needs: publish`. Clean venv; poll the real index until all
   three resolve (propagation lag, bounded ~10 min); `pip install
   omnigent==X omnigent-client==X omnigent-ui-sdk==X` (exact rc pins resolve
   without `--pre`); assert `omnigent --version` == X; import smoke. Replaces
   the manual venv recipe.

`destination=test-pypi` and `dry-run=true` inputs stay for rehearsals, but the
standard flow no longer uses TestPyPI (per new policy: rc goes to real PyPI as a
PEP 440 prerelease, which default `pip install omnigent` never resolves — safer
than the TestPyPI dependency-confusion dance RELEASING.md currently documents).

Net: one dispatch, one approval click, per phase.

## Workflow 3 — `finalize-release.yml` (new, final releases only)

`workflow_dispatch` input: `tag` (e.g. `v0.6.0`).

1. **checks** (all fail with actionable links):
   - tag is a final `vX.Y.Z`; a *draft* GH release exists for it;
   - PyPI serves all three packages at the version (JSON API) — never publish
     release notes for something uninstallable;
   - the `auto/changelog/vX.Y.Z` CHANGELOG PR is merged;
   - **docs sweep**: zero open PRs in `omnigent-site` with base `X.Y-docs` —
     the deterministic form of "all release docs PRs reviewed + merged/closed".
     Each open PR is listed in the summary; resolving them stays human work.
2. **publish** behind a `publish-release` environment (required reviewer).
   Approving *is* the attestation "I reviewed/curated the draft notes."
   Then, with the App token: `gh release edit vX.Y.Z --draft=false --latest`.
   Two footguns handled here that have bitten before: `--latest` must be
   explicit (API publishes don't set `make_latest`), and the App token (not
   `GITHUB_TOKEN`) ensures the `release: published` event actually fires
   `publish-changelog.yml`, which opens the site release-post PR and the
   `X.Y-docs → main` docs-publish PR.
3. **summary**: links to the two site PRs awaiting merge.

rc releases never finalize: their GH drafts stay unpublished prerelease drafts
(**decided**: keep exactly today's pattern — rc drafts are never published on
GitHub).

## Workflow 4 — `update-homebrew.yml` (new, final releases only)

Current state of `omnigent-ai/homebrew-tap`: a homebrew-core-style tap that is
already 2/3 automated —

- `Formula/omnigent.rb`: `Language::Python::Virtualenv` formula; stable
  installs the **PyPI sdist** (url + sha256) with **94 pinned Python
  resources**; a few deps come from brewed formulae instead
  (`certifi`/`cryptography`/`pydantic`/`rpds-py` as `:no_linkage`, plus
  `python@3.14`, `libyaml`, `tmux`, Rust build deps); hand-maintained
  platform-conditional `google-antigravity` wheel stanzas; bottles hosted on
  the tap's GitHub releases.
- `tests.yml`: `brew test-bot` on 3 macOS runners — on every PR it builds the
  formula (i.e. builds the bottles) and uploads them as artifacts.
- `publish.yml`: on the `pr-pull` label, `brew pr-pull` publishes the bottles
  to a tap release, rewrites the bottle block, merges to main.

The **only missing link is the bump PR** — nobody opens it, which is exactly
why the tap froze at 0.2.0 (2026-06-23) while PyPI moved to 0.5.1. The
`omnigent-desktop` cask needs nothing: it is `version :latest` /
`sha256 :no_check` against `omnigent.ai/download/mac`, i.e. evergreen.

New workflow in omnigent-ai/omnigent, shaped exactly like
`publish-changelog.yml` (event + dispatch fallback, App token, idempotent
PR-opening):

- Triggers: `release: types: [published]` (fires automatically from
  finalize's App-token publish; guarded to final `vX.Y.Z` like
  publish-changelog) + `workflow_dispatch(tag)` for retries and catch-up.
- Steps: bounded-poll the PyPI JSON API until the new sdist is visible; on a
  macOS runner with `Homebrew/actions/setup-homebrew`, check out the tap via
  an App token (App installed on `homebrew-tap`); rewrite `url`/`sha256` from
  the PyPI metadata and drop any `revision`; regenerate the resource pins with
  `brew update-python-resources` (excluding the brewed-formula deps and the
  hand-maintained `google-antigravity` stanzas so they're preserved); run
  `brew style`/`brew audit` as a sanity gate; push `bump-omnigent-<version>`
  and open (or update) the tap PR.
- From there the tap's own machinery takes over: test-bot builds the bottles
  on the PR; a human reviews the resource diff and applies `pr-pull`; the
  existing publish workflow bottles + merges. One review + one label click per
  final release — the human gate the tap already has, kept.

First run doubles as the **catch-up**: dispatch with `tag=v0.5.1` to jump the
formula 0.2.0 → 0.5.1 (expect that one resource diff to be large).

## Who can trigger a release (maintainer-only)

`workflow_dispatch` is runnable by anyone with write access, which is too
broad. Every release workflow (`release.yml`, `finalize-release.yml`,
`update-homebrew.yml`'s dispatch path) gets a first `authorize` job that all
other jobs `need`:

```
role=$(gh api "repos/$GITHUB_REPOSITORY/collaborators/${GITHUB_ACTOR}/permission" --jq .role_name)
case "$role" in admin|maintain) ;; *) fail "release workflows require maintain/admin" ;; esac
```

`github.actor` on a dispatch is the dispatcher and can't be spoofed; roles
come from repo settings, so there's no hand-kept allowlist to rot. Defense in
depth stacks three independent layers: this actor gate (highest repo
privilege to start anything), the `v[0-9]*` **tag ruleset** (create/update/
delete restricted to the omnigent-ci App + admins — even a bypassed workflow
can't tag; goose's primary gate), and the secure repo's own access model
(admin/maintain to dispatch, environment reviewers on the upload). The
alternative — a required-reviewer environment on the first job — adds an
approval click and a separately-maintained reviewer list for no additional
precision; rejected.

## What stays human, on purpose

1. Choosing version/timing/base commit (the dispatches).
2. Secure-repo environment approval — publish authorization.
3. Release-notes curation + the finalize approval that attests to it.
4. Content review merges: CHANGELOG PR, bump-main PR, doc PRs on `X.Y-docs`,
   the release-post PR, the docs-publish PR.
4a. The homebrew-tap bump PR: review the resource diff, apply `pr-pull`.
5. Yank decisions when something shipped broken (policy unchanged: never reuse
   a version; `skip-existing` re-runs heal *partial* publishes, yank handles
   *bad* ones).

## Recovery model

Any run can be re-dispatched with identical inputs after any failure; every
step converges or fails loudly rather than duplicating. Pre-publish mistakes
(wrong commit tagged): delete tag + draft, re-dispatch — unchanged from
RELEASING.md. Post-publish: fix forward to the next version.

## Cleanups this unlocks

- **Delete `release-omnigent.yml`** — its own header says "to be deleted once
  the secure path has done a prod release", which has now happened repeatedly.
  Also retire its `pypi`/`test-pypi` Trusted Publishers on PyPI: a live trusted
  publisher pointing at the public repo is standing attack surface.
- Rewrite `RELEASING.md` around the dispatches, demoting today's CLI runbook to
  a break-glass appendix. The `uv.lock` hand-edit instructions disappear.

## What peer projects do (survey, 2026-07)

### pi (`earendil-works/pi`)

Lean solo-maintainer automation, no release branches, no rc channel — cadence
(a release every 1–2 days) substitutes for candidates. Mechanics worth noting:

- **Draft-then-flip**: binaries staged on a *draft* GH release; the release is
  made public only after npm publish succeeds; any failure deletes the draft;
  the workflow *refuses to mutate an already-published release*.
- **Idempotent publish**: `npm view <pkg>@<ver>` before every upload, skip if
  present — re-running a tag workflow after a partial failure heals it.
  (The direct inspiration for the `skip-existing` PyPI probe above.)
- **Recovery dispatch**: the tag-triggered build workflow has a
  `workflow_dispatch` twin with `tag` + `source_ref`, labeled "release
  recovery only".
- Lockstep versions across 4 npm packages enforced by one sync script with a
  check mode (their `sync-versions.js` ≈ our `update_versions.py`).
- Release notes: maintainer runs pi's own `/cl` prompt to audit CHANGELOG
  entries with a human-confirm step — the same posture as our
  `draft-release-notes.yml` + human curation.
- Pre-publish smoke is a *manual* isolated-install checklist in AGENTS.md;
  **no automated post-publish validation exists** in their CI.

### opencode (`anomalyco/opencode`)

Continuous-publish machine: every push to `dev` ships an npm prerelease under
a branch-named dist-tag; an hourly bot assembles a `beta` branch (with their
own agent resolving merge conflicts); a real "latest" release is **one
`workflow_dispatch` click** (bump dropdown) — build, sign, notarize, npm,
Docker, AUR, Homebrew, LLM-authored release notes, Discord announce, all
unattended. Relevant mechanics:

- Bot pushes via a **GitHub App token** (`create-github-app-token`), never a
  PAT — same identity pattern as our omnigent-ci App.
- Same idempotent already-published-skip before every npm publish.
- npm auth is OIDC trusted publishing, zero registry tokens in CI.
- Fully autonomous LLM changelog with *no* human review gate, and no
  environment protection on the publish job at all — a rigor level below what
  a Databricks-governed project should copy.
- Docs are evergreen/unversioned, deployed on push, fully decoupled from
  releases.

### Cross-cutting (both)

- **Neither peer automates post-publish validation** (clean-env install of
  the just-published artifact + run it). The `validate` job in the secure repo
  puts omnigent ahead of both, not just at parity.
- **Neither has an rc→final concept** — both rebuild rather than promote.
  Rebuilding the final from the same `release/vX.Y.0` (rather than promoting rc
  artifacts) is also what our model does; PyPI's no-reupload rule makes
  rebuild-and-restamp the pragmatic norm.
- Both decouple docs publishing from the release pipeline structurally — which
  supports keeping our site PRs as separate human-reviewed merges rather than
  folding them into `release.yml`.

### cline (`cline/cline`)

Three independent release trains (VS Code extension, CLI, SDK), all
`workflow_dispatch`, all preconditioned on a *human-authored* version-bump +
changelog PR — despite appearances, no bot writes their bumps. Worth stealing:

- **Tag/SHA idempotency guard** (`ext-vscode-publish-stable.yml`, "Resolve
  Release Tag"): tag exists → assert it points at the tested SHA (no-op on
  match, hard-fail on mismatch); tag absent → create it from the tested SHA
  after asserting that SHA is an ancestor of `main`. Verbatim the semantics
  `release.yml`'s plan/execute jobs adopt.
- **Gate placement**: the named-required-reviewer GitHub Environment guards
  *only* the VS Code Marketplace publish (highest blast radius); CLI/SDK get a
  typed `confirm_publish: "publish"` string. Principle: spend the heavyweight
  second-person gate on the irreversible step only — for omnigent, that is the
  secure-repo PyPI upload, which already has exactly such an environment.
- **Changelog-as-gate**: publish hard-fails if the changelog's top entry ≠ the
  version, then reuses that section as the release body (and a Slack post).
  Our equivalent is finalize's "CHANGELOG PR merged" check.
- No release branches, no rc versions (marketplace "pre-release" is a flag on
  a normal version), no post-publish validation, no rollback story.

### kilocode (`Kilo-Org/kilocode`)

Product forked from cline, but the *release pipeline* is forked from opencode
(they even poll `anomalyco/opencode` releases to sync). Main train: **one
dispatch** (`bump` dropdown, `pre_release` defaults true) → version → build →
**validate matrix** (executes the built binary on macOS/Linux/Windows/Alpine)
→ **smoke-test** (real eval tasks against the *draft release's* assets) →
unattended publish to npm/Marketplace/GHCR/AUR/brew. No environment gate at
all on that train — below the rigor a Databricks-governed project should copy.
The interesting part is the **JetBrains train**, the only peer flow with true
rc→stable promotion: `prepare-jetbrains-release.yml` (`kind: rc|stable`,
`version`, `from_tag`) opens a release branch + PR; the human *merge* of that
PR is the approval gate; `publish-jetbrains.yml` fires on the merge, with a
dispatch fallback for re-runs; rc tags chain `-rc.1 … -rc.15 → stable`.

**Considered variant for omnigent** (from the JetBrains pattern): have
`release.yml` open a bump *PR* onto `release/vX.Y.0` instead of pushing directly,
making the merge a second-person cut-approval and running CI on the bump
commit. Rejected as the default: the bump is deterministic robot output
(`update_versions.py` + `check`), the cut is fully reversible, the secure
repo's gates re-verify everything against the tag before anything publishes,
and the extra merge per rc works against the 1–2-runs goal. Easy to switch to
later if a second-person cut gate is ever wanted.

### goose (`block/goose` → now `aaif-goose/goose`)

The closest org-shape analogue (big-company compliance, busy monorepo,
canary + stable channels, release branches). Minor release = weekly scheduled
bump PR → human merge → auto-cut `release/X.Y.0` + release PR → human runs two
copy-pasted `git tag && git push` commands → everything downstream (10-platform
build, signing, GHCR + SLSA, LLM release notes, Discord, auto-created next
hotfix branch) is automatic. ~5 human actions per minor. Findings that matter:

- **Their gate is a repo-wide tag-protection ruleset** (create/update/delete
  blocked on *all* tags without bypass privilege), not environment reviewers —
  environments are used only to scope secrets. Cheap, auditable.
- **They hit the `GITHUB_TOKEN` event-suppression gotcha in production**:
  their LLM release-notes workflow runs on `workflow_run` *specifically*
  because `release: published` doesn't fire for token-authored releases — the
  same trap our App-token choices are designed around (and that
  `draft-release-notes.yml` already dodges the same way).
- Their SDK packages **silently drifted out of lockstep** because nothing
  asserts it — the failure mode our `update_versions.py check` prevents, and
  an argument for running it in CI permanently (see hardening below).
- Canary = a single floating GH release overwritten in place; promotion is
  always rebuild-from-source, never relabel.
- No dry-run, no post-publish validation, dependency scan *not* wired as a
  publish gate, idempotency uneven, no rollback runbook.

### hermes (`NousResearch/hermes-agent`)

Real and public. CalVer tags (`v2026.7.7.2`), no release branches, no rc
channel, weekly cadence with same-day suffixed hotfixes; releasing is a local
`release.py` a maintainer runs (~3 actions), with GH Actions as reactive side
effects. Worth stealing:

- **Lockstep-as-a-test**: a real CI test asserts their four version locations
  agree — drift is caught structurally no matter how it happened (bad merge,
  cherry-pick, manual edit), not just when the bump script runs.
- **PyPI publish uses `skip-existing: true`** (pypa action) — direct precedent
  for the partial-publish healing proposed for the secure repo.
- **Re-publish escape hatch**: `upload_to_pypi.yml` has a dispatch with a
  `confirm_tag` input documented as "re-publish an existing tag" — the
  idempotent-retry shape our secure-repo dispatch already has via `ref`.
- Bounded poll-with-warning (not hard-fail) when reading back a just-created
  release/tag that may lag — adopted in the `validate` job's PyPI polling.
- Cautionary tale: their dependency-manifest review ruleset was empirically
  self-merged around on a real release PR — review gates that the same person
  can approve are decoration. (The secure repo's separate-org reviewer set
  doesn't have this hole; keep it that way.)

### Cross-cutting (all six)

- **Nobody automates post-publish validation** — the secure repo `validate`
  job is ahead of every peer surveyed.
- **Nobody has versioned docs** — all continuous-deploy latest-only. The
  `X.Y-docs` staging design has no prior art to borrow; it's already built and
  just needs the sweep gate.
- **Nobody has a backport/patch-branch story** as good as `release/vX.Y.0` +
  cherry-pick; cline maintains one frozen legacy branch, kilocode has nothing.
- Pre-publish smoke against built artifacts (kilocode) ≈ the secure repo's
  existing smoke-install gate. Parity, not a gap.
- **Nobody documents rollback/yank** — RELEASING.md's recovery section is
  ahead of all six; the new workflows keep it (and make partial-publish
  recovery automatic via skip-existing).
- rc→final promotion is rebuild-from-the-pinned-ref everywhere it exists at
  all (goose canary→stable, kilocode JetBrains) — never artifact relabeling.
  Validates our model: the final independently re-runs build+scan+publish
  from `release/vX.Y.0`, which the mandatory dependency scan requires anyway.
- omnigent's mandatory scan-gates-publish + separate-org publisher is
  **stricter than every peer surveyed** (goose's scan isn't a gate; hermes's
  review gate was self-merged around; opencode/kilocode publish unattended).

## Hardening extras (cheap, independent of the workflows)

- **Run `update_versions.py check` in CI permanently** (a test or `ci.yml`
  step), not just inside bump/release workflows — goose's SDKs silently
  drifted out of lockstep for lack of exactly this assertion (hermes has it
  and it works).
- **Tag ruleset on `v[0-9]*`**: restrict create/update/delete to maintainers +
  the omnigent-ci App. Today any write-access account can push a version tag
  and set off the draft-release + docker-publish chain; goose treats tag
  protection as their primary release gate.

## Decisions (2026-07-14)

> **Correction (2026-07-16, after two live failures):** the
> skip-existing / partial-publish-healing idea below is **withdrawn**. Every
> skip mechanism must first *read* the index, and the release runners have
> no egress to pypi.org's JSON API — the curl probe silently never matched,
> and twine's `--skip-existing` pre-checks that same API client-side and
> crashed every upload (secure-repo run 29459796204), including brand-new
> versions. The publish leg is **write-only**: re-uploads hard-fail
> ("File already exists") and a partial publish is recovered by yank + next
> version, as it always was. The peer-survey skip-existing citations stand
> as facts about those projects; they don't transfer to egress-restricted
> runners. The **`validate` job is withdrawn for the same reason**: the
> runners' only index view is a JFrog mirror whose metadata lags weeks
> behind PyPI (its first live run couldn't see the version it had just
> published — nor even 0.5.x), so post-publish validation stays the manual
> runbook step, run from a network with a fresh PyPI view.

1. **Secure-repo restructure: approved direction** — gates → env-approval →
   publish (skip-existing — *withdrawn, see correction above*) → validate,
   one dispatch per phase.
2. **rc GH drafts are never published** — keep today's pattern exactly.
3. **bump PRs move to the App token** so CI runs on them (empty-commit
   workaround retired).
4. **Release workflows are maintainer-only**: `authorize` actor-role gate
   (admin/maintain) + the `v[0-9]*` tag ruleset as backstop.
5. **Homebrew joins the pipeline** via `update-homebrew.yml` on
   `release: published`; tap-side human gate (`pr-pull` label) kept.

## Open questions

1. Environment `publish-release` reviewer set = who may finalize a release.
2. `brew update-python-resources` vs. the hand-maintained formula sections:
   confirm on the catch-up run that the exclusion flags preserve the
   `google-antigravity` platform stanzas and the brewed-dep comments, or keep
   those sections behind guard comments the updater skips.
3. Tap bottle coverage (currently arm64 macOS only) — widen the test-bot
   matrix? Orthogonal to this pipeline; tracked here so it isn't forgotten.
