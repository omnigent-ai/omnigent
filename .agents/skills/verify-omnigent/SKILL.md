---
name: verify-omnigent
description: Verifies every affected Omnigent product surface through fail-closed CLI, Electron, Playwright, server, and harness/client profiles, with optional live and Databricks/Universe compatibility checks. Use before implementation planning and before declaring an Omnigent change complete.
---

# Verify Omnigent

Turn the feature request into proof obligations, then verify each affected
product surface. Use unit and component tests as fast feedback, not as a
substitute for a real CLI, Electron, browser, server, or client journey.

## Plan before implementation

Write this verification contract before changing code:

```text
Feature:
Acceptance criteria:
- Given <starting state>, when <user action>, then <observable result>.

Affected surfaces:
- desktop | cli | web-ui | server | harness-client

Compatibility matrix:
- current client / current server:
- older client / new server:
- new client / older server:
- downstream Universe, if explicitly requested:

Latency budget:
- journey and measurement:
- baseline:
- maximum allowed regression:
- if no baseline exists, state that limitation:

Proof obligations:
- profile or focused test for each acceptance criterion:
- persistent/API read-back for each write:
- negative proof for disabled or dry-run behavior:
- credentialed, platform-specific, or downstream lanes that need explicit opt-in:
```

Do not start implementation with "tests pass" as the acceptance criterion.
Name the user action, visible or API result, compatibility combinations, latency
measurement, and evidence file that will prove each claim.

Read [correctness.md](correctness.md). Write the observable invariant and its
failure cases before choosing tests. Use the lowest test level that can falsify
the claim. Do not add a standalone E2E test for a render-only condition already
covered by a component test.

## Choose the verification lanes

Read the diff, then select every applicable local lane:

- Any product or test code: run `quality-gates`. It runs pre-commit on changed
  files, plus the full web format, lint, type-check, and production-build
  commands when web code changed.
- `web/**`: web unit tests plus the closest mapped Playwright journey.
- `omnigent/server/**`, `omnigent/runtime/**`, or `sdks/**`: server/SDK tests,
  the chat smoke journey, and compatibility checks.
- DB models, store interfaces, or migrations: migration tests and a downstream
  risk assessment. Run the Universe lane only when the user explicitly opts in.
- auth, account-auth, identity, session authorization, sharing, permissions,
  roles, login/registration pages, OIDC/account stores, or their client
  surfaces: collaboration journeys plus the applicable server or UI lane and a
  downstream risk assessment. Run the Universe lane only when explicitly asked.
- scheduled tasks: run the automations profile, but preserve capability gating;
  managed Databricks deployments intentionally disable automations.
- user-visible render/load changes: record timings and the request waterfall
  from the same Playwright journey before and after. Do not claim "no latency
  regression" from a passing functional test.
- CLI setup/onboarding changes: use the existing project
  `.claude/skills/cli-setup-verify` PTY driver with isolated HOME, config, and
  data directories.
- harness adapter changes: run the relevant E2E skill plus
  `tests/harness_bench`; use `--no-live` first and a real credentialed canary
  only when the change warrants it.
- desktop/mobile shell changes: run their Playwright/native wrapper journey in
  addition to the shared web path.
- deployment dry-runs: verify the promised non-effects on files, network,
  browser launches, database state, and git refs.

## Launch

For the normal one-shot path, compare the checkout to the intended base:

```bash
OMNIGENT_VERIFY_ADAPTER=cursor \
  .agents/skills/verify-omnigent/scripts/verify.sh auto --base-ref origin/main
```

Set `OMNIGENT_VERIFY_ADAPTER` to the invoking adapter: `cursor`,
`claude-code`, `codex`, or `omnigent`. The value is self-reported in evidence;
if it is absent or invalid, the manifest records `unspecified` or `invalid`
and adds a limitation instead of guessing.

`auto` resolves the merge base between `HEAD` and the requested base, reads
committed branch changes from that branch point, then includes staged, unstaged,
and untracked paths exactly once. It records why each lane was selected, runs
required lanes sequentially, and writes a separate child manifest for each lane.
A missing base ref, unresolved or ambiguous merge base, missing prerequisite,
unmapped product path, missing child manifest, skipped required live probe, or
failed lane makes the one-shot command nonzero. Set
`OMNIGENT_VERIFY_BASE_REF` instead of `--base-ref` when the caller cannot pass an
argument. The default base is `HEAD`.

Before starting any selected lane, `auto` checks every selected lane's
prerequisites. A blocker finalizes the parent and every selected child entry as
`blocked`; it does not start a partial run. Required lanes then run fail-fast.
If a required server, harness-client, or CLI step fails, `auto` reruns only that
failed step at the unique merge base in a detached temporary worktree when
dependency inputs match. Quality-gates, web-ui, and desktop exact-base
comparisons are explicitly unsupported without isolated JavaScript dependencies
and are classified `could_not_compare`; they are never presented as reproduced
or PR-only failures. The
classification is `pr_only_failure`, `baseline_reproduced`,
`baseline_differs`, or `could_not_compare`. A reproduced baseline failure is
still a required failure and never makes the parent pass. Partial execution is
authorized by a private, signed, one-use request generated by that `auto` run;
caller-supplied partial-execution environment variables are rejected. The
comparison authenticates `steps.json` and its logs against the finalized child
manifest and freshly generated profile commands before classifying anything.

To verify another clean checkout without copying the skill into it, run the
canonical script with an explicit repository root:

```bash
OMNIGENT_VERIFY_REPO_ROOT=/path/to/omnigent-checkout \
OMNIGENT_VERIFY_ADAPTER=cursor \
  .agents/skills/verify-omnigent/scripts/verify.sh auto --base-ref origin/main
```

Use `all-surfaces` only when you intentionally want every local,
credential-free product lane:

```bash
OMNIGENT_VERIFY_ADAPTER=cursor \
  .agents/skills/verify-omnigent/scripts/verify.sh all-surfaces
```

The script uses the repository's `tests/e2e_ui` fixtures. They build the real
Vite bundle, start a mock OpenAI-compatible LLM, start `omnigent server` and a
runner on random ports, and allocate a temporary SQLite DB and artifact
directory. Readiness means `/health` is 200 and the fixture-owned runner reports
online. No provider credentials are needed and product traffic stays on
loopback. First-time dependency or browser setup can use package registries;
`backend` intentionally installs into disposable virtual environments and
therefore requires package-network access.

Available profiles:

- `auto`: select required product lanes from the diff against a configurable
  base ref.
- `all-surfaces`: run `quality-gates`, `server`, `harness-client`, `cli`,
  `web-ui`, and `desktop` sequentially.
- `quality-gates`: run pre-commit on changed files in a disposable worktree.
  Pinned remote hook repositories are copied selectively from a trusted user
  cache into the disposable runtime; a missing exact revision is an actionable
  offline prerequisite. Changed verification, deploy, and Playwright evidence
  contracts run explicitly. Web changes also run the full web format, lint,
  type-check, and production-build binaries without package installation.
- `prepare-hooks`: explicitly prepare the exact remote repositories and
  revisions in `.pre-commit-config.yaml`, authenticate their trees, and
  atomically publish them to `~/.cache/verify-omnigent/pre-commit`. This is a
  distinct network-capable bootstrap operation, never verification evidence:
  `.agents/skills/verify-omnigent/scripts/verify.sh prepare-hooks`.
- `universe`: explicit non-syncing downstream preflight. It discovers a sibling
  Universe checkout, compares its MAS pins with `--oss-ref` or the current OSS
  `HEAD`, scopes affected patches with `--base-ref`, checks them in a temporary
  tree, and runs applicable hermetic Bazel gates. It never syncs or edits
  tracked Universe source; see the ignored-output limitation below.
- `server`: app and API integration tests from the existing project
  environment. It covers health, API-only landing, static routing, auth mode,
  host routes, and app assembly without package-network access.
- `backend`: opt-in clean-room API smoke against `/`, `/health`, `/docs`,
  `/v1/agents`, and `/v1/sessions`. It installs into disposable environments
  and requires package-network access.
- `cli`: isolated PTY checks for config isolation, cold start, top-level help,
  and server help. It requires macOS or Linux and never inherits the user's
  HOME or credentials.
- `web-ui`: web unit tests, type checking, and the real core Playwright journey.
- `desktop`: Electron unit tests, JavaScript Electron Playwright journeys,
  browser desktop-mode Playwright, and an unsigned package for the current
  platform. Signing and notarization are not claimed.
- `harness-client`: harness bench tests plus the credential-free offline
  capability matrix. It does not claim a live turn.
- `harness-live`: explicit credentialed canary for one harness. Set
  `OMNIGENT_VERIFY_HARNESS`; optionally set
  `OMNIGENT_VERIFY_DATABRICKS_PROFILE`. A skipped or missing basic turn fails.
- `perf`: repeatable session-list, history-load, and time-to-first-token
  benchmark with request-count and latency JSON. Pass `--base-ref REF` for a
  bounded matched base/candidate comparison. Parent-owned defaults reject p50
  or p99 regressions above 10%; override them explicitly with
  `--max-p50-regression-percent` and `--max-p99-regression-percent`. Thresholds
  emitted by benchmark code are ignored.
- `smoke`: send a message through the real SPA, SSE, server, runner, and SDK
  reducer.
- `collaboration`: permission-modal controls, sharing lifecycle, read-only and
  sharing-off behavior, realtime collaborator updates, and author attribution.
- `automations`: Scheduled Tasks UI and REST persistence.
- `db-migration-deploy`: focused database migration, store, and deployment
  contracts. `auto` selects it only for DB/schema/store/deploy changes.
- `perf`: is selected by `auto` only for mapped latency-sensitive session,
  conversation, streaming, or benchmark paths; it is not added to a broad
  `all-surfaces` run.
- `core`: smoke, new-session UI, file autosave, and collaboration.
- `full-ui`: all non-visual UI E2E tests.

First-time setup:

```bash
uv sync --locked --extra all --extra dev
CI=true pnpm install --frozen-lockfile
uv run --no-sync playwright install chromium
```

## Doctor

Run doctor for the exact profile you plan to drive:

```bash
OMNIGENT_VERIFY_ADAPTER=cursor \
  .agents/skills/verify-omnigent/scripts/verify.sh doctor auto --base-ref origin/main
```

The target profile defaults to `smoke`. Doctor checks only that profile's
requirements. `doctor auto` computes the same lane plan and checks every
selected lane. `doctor all-surfaces` checks all six local lanes. Each blocker
includes the exact setup command or platform requirement. Fix doctor failures
before interpreting product failures. Doctor attempts also create and finalize
evidence manifests.

Doctor rejects `node_modules` metadata owned by another checkout. Verification
never answers pnpm's purge prompt, installs packages, or repairs the dependency
tree. Verification Vite builds use the supported `runner` config loader and
run-owned output/runtime roots, so config loading never creates
`node_modules/.vite-temp`.

Run `.agents/skills/verify-omnigent/scripts/verify.sh prepare-tools` explicitly
to prepare the exact `packageManager` pnpm pin. Preparation prefers an existing
matching Corepack or pnpm tools package; only this explicit command may ask
Corepack to obtain the exact pinned version when it is absent. It copies only
that pnpm package into a sealed, atomically published user cache. Normal
verification holds a shared cache lease while authenticating and staging the
runtime into its disposable cache, prepends only that staged executable to
`PATH`, and remains offline.

Python verification commands use `uv run --no-sync`; quality steps use an
isolated HOME and a private cache containing only exact configured remote hooks
copied from the authenticated dedicated cache. Runtime caches stay outside
persistent evidence and are verified deleted. Verification uses offline/frozen
settings, `CI=true`, and no ambient credentials. Every non-credentialed doctor,
direct lane, composite
lane, child, and comparison uses the same environment allowlist. `harness-live`
receives one labeled credential mode. When a Databricks profile is explicitly
selected, verification copies only that profile into the disposable home and
strips ambient OpenAI, Databricks token/host, Cursor, and other provider
credentials. Without a profile, a complete ambient OpenAI pair takes precedence
over a complete Databricks host/token pair; a Cursor API key is forwarded only
for an explicitly selected Cursor harness when neither pair is present.
`steps.json` records the mode used, never its values. Credential values are
never recorded in evidence.
Disposable runtime homes and caches are verified absent before evidence
finalization and are never inventoried as persistent artifacts.

Verifier control metadata and the authoritative pre-run snapshot live in a
private parent-owned directory that product commands do not receive. Managed
children cannot delete or replace the active manifest without forcing failure.
Repository invariants include file bytes, dependency roots, build outputs,
lockfiles, refs, raw/symbolic HEAD, and index identity. Shared dependency trees
are recursively authenticated with kernel-controlled identity/change metadata.
Potentially mutating quality hooks receive copy-on-write dependency trees, not
writable links to the primary checkout; verification blocks when the platform
cannot provide safe clone semantics.
Tracked or otherwise visible symlinks that resolve outside the checkout block
before execution. Verification Vite builds use a fresh run-owned output;
the output and every ancestor are checked with `lstat` immediately before Vite
can empty it.

Never point verification at a user's dev server by default. The E2E harness
rejects known dev ports because tests mutate sessions, files, permissions, and
database state. Only use `--ui-base-url` for deliberate local debugging with
`OMNIGENT_E2E_ALLOW_DEV_BASE_URL=1`, and never against production.

## Drive

1. Read [features/README.md](features/README.md) and choose every entry point
   affected by the change.
2. Run the smallest profile that exercises a real user path.
3. Treat Playwright as the authoritative browser proof. Omnigent browser tools
   are useful for exploratory debugging, but their output does not replace the
   repository's Playwright journey.
4. Use stable ARIA names, placeholders, and `data-testid` selectors already
   present in `tests/e2e_ui`; do not use coordinates or tab order.
5. For a bug, follow [repro-resolve.md](repro-resolve.md): reconstruct the
   reported journey, reproduce live, give a verdict per symptom, then add or
   strengthen a durable E2E test before fixing.
6. After the focused proof, run `quality-gates` or the `auto` profile. Do not
   declare a change verified while a merge-critical format, lint, type-check,
   build, or pre-commit command is failing.

### Launch the desktop app manually

From the repository root of the exact branch or worktree under test:

```bash
just electron-dev
```

Use the real Electron shell for OS-level behavior such as menu accelerators,
window focus, and SSO. A normal browser or Vite tab cannot verify Electron's
main process. Record the worktree and the narrow manual action in the report;
the automated `desktop` profile remains required.

For browser/server compatibility, prefer additive API evolution:

- New response fields must be optional to old clients.
- Clients must default safely when a capability or field is absent.
- Do not remove or reinterpret an endpoint/event until supported client
  versions have aged out.
- Keep TypeScript stream reducers behaviorally aligned with the Python SDK.
- Gate UI affordances from `/v1/info`; the server must enforce the same rule.

## Evidence

Every attempted script run writes a collision-safe directory under
`.artifacts/verify-omnigent/`. `manifest.json` is schema version 1 and is
atomically rewritten while running and on success, failure, `INT`, `TERM`, or
`HUP`. Final statuses are `passed`, `failed`, `interrupted`, or `blocked`; no
normal or handled-signal exit may leave `running`. It records status, profile,
reproducible argv with repository/run roots tokenized, the self-reported adapter,
repository commit and dirty state, timestamps, JUnit-derived per-test results
when available, cleanup confirmation, `downstream_universe.status`, explicit
limitations, and every regular artifact's relative path, MIME type, SHA-256,
byte count, and authority. It never embeds binary/base64 payloads or absolute
artifact paths. Symlinks and unreadable or escaped files are excluded and
reported as limitations. A strict before/after snapshot distinguishes existing
dirty state from new changes, hashes lockfiles and dependency metadata, and
forces a blocked/failed result if either snapshot is unreadable or verification
mutates the checkout.

Orchestration manifests add the changed-file decision and a `children` entry
for every required lane before startup. Each entry records startup blockers,
the child manifest's relative path, SHA-256, status, exit status, and any
baseline classification. Parent finalization reconciles interrupted or stale
children and records cleanup. The parent does not inventory or relabel child
artifacts. Read the hashed child manifest for that lane's authoritative tests
and artifacts.

When `auto` or `all-surfaces` receives `--with-universe`, it adds the `universe`
child. The child records source patch application separately from runtime proof.
A pin mismatch records `downstream_universe.status=not_synced` and fails the
required lane even when every source patch applies.

`run.log` is the complete runner log. Composite profiles also write
`steps.json` with each exact command and exit status. UI profiles request JUnit,
screenshots, traces, and failure videos. Evidence is **test-bound** when the
repository's pytest run owns the browser context and artifact lifecycle:

- pytest-playwright `page` / `context` fixtures and direct synchronous
  `browser.new_context()` / `browser.new_page()` calls share the plugin's
  screenshot, trace, and failure-video recorder;
- direct async `async_playwright()` browser launches are wrapped during a
  verification run and capture screenshots, traces, and videos;
- each observed browser context emits bounded, redacted console, page-error,
  request, response, and request-failure metadata. It records method, resource
  type, status, and query-free URL only—never headers or bodies. Text redaction
  is heuristic, so the manifest tells recipients to review evidence before
  sharing it outside the verification boundary;
- trace ZIP sanitization streams each member under per-member, cumulative
  expanded-byte, member-count, top-level, and nesting limits. A limit breach
  removes the suspect context trace and fails evidence validation.

Managed web and desktop profile runners pass only their run-owned
`cleanup.json` path into nested pytest environments. A successful UI profile
requires a versioned marker confirming completed pytest session cleanup and a
zero pytest exit status; missing, malformed, stale, or cross-run markers fail
the profile.

Tests that create no browser remain normal tests and contribute JUnit results,
not fabricated browser files. A UI command with exit status zero still fails
verification when its JUnit report, context metadata, screenshot, or trace is
missing. Missing failure video remains an explicit limitation.

Browser-tool screenshots made outside pytest are **supplemental**. Place them
under the run's `supplemental/` directory before finalization to inventory them
as `supplemental-browser-tool`. They can explain exploratory work, but cannot
replace or acquire the authority of a test-bound Playwright journey.

A valid proof:

- exercises the real user path, not internal setters or test-only endpoints;
- captures both the action and resulting UI state;
- reads back persistent/API side effects when the feature writes state;
- uses mocks only at an existing production boundary, such as the E2E mock LLM;
- records the exact profile and commit/worktree tested.

For dry-run or disabled modes, assert what did not happen: no row, file, network
call, grant, or ref mutation. Never trust a mode name alone.

For UI latency, compare the same journey and environment. Capture navigation,
time-to-interactive or user-visible completion, request count, and any new
long-running main-thread work. If no baseline exists, report that limitation;
do not invent a threshold.

Run `.agents/skills/verify-omnigent/scripts/verify.sh perf` before and after
changes to session listing, history, or turn startup. Compare `benchmark.json`
by journey and backend, including p50/p99, `avg_http_requests_per_op`, and
`network_routes`. Use a seeded corpus and the same database backend for
meaningful release gates. Default SQLite is a low-noise development signal, not
a production latency claim.

For a first-class matched comparison:

```bash
.agents/skills/verify-omnigent/scripts/verify.sh perf --base-ref origin/main
```

The comparison records both commits, config and seed identity, p50/p99, request
counts, routes, failures, backend, harness, and host identity. It blocks when
those inputs do not match. Functional success is separate from latency: every
journey must run, `runs_ok` must equal a positive `runs_total`, and every run
must report `n_failures=0`. Without explicit applicable p50 and p99 regression
thresholds, the comparison records deltas but remains blocked; it cannot produce
a green verification result.

## Report

Report every acceptance criterion as `passed`, `failed`, or `not run`. Name the
exact test and child manifest that support the result. Passing a broad profile
does not prove a criterion unless one of its tests exercises that behavior.

List manual-only checks separately. For each one, give the exact action, why
automation cannot reproduce it, the automated precursor that already passed,
and the failure signal the user should look for. Do not ask for a second manual
pass after a test-only or history-only change when the product diff is
unchanged.

Separate correctness blockers from optional confidence checks. A skipped
required lane is a blocker, not a pass with a caveat.

`auto` ends with a compact terminal summary: selected lanes, child status and
manifest path, baseline classification, cleanup, and the exact next command.
Full command output remains in `run.log` and per-step logs.

## Cleanup

Pytest owns every server, runner, mock LLM, random port, temporary database, and
artifact directory. Its fixture finalizers terminate only the PIDs they started
and remove scratch state. Never kill by process name.

Every command, lane child, and comparison helper runs in a dedicated managed
session. The runner records descendants and also tracks an inherited private
file descriptor so a fast double-fork into another session remains discoverable.
On interruption or timeout it signals and reaps the recorded/marked tree with
bounded TERM/KILL escalation, then atomically finalizes the manifest. A hostile
child that deliberately closes every inherited descriptor before escaping can
still outrun ancestry observation on platforms without cgroup/subreaper
containment; verification does not claim a hard OS sandbox. Cleanup remains
`unknown` unless
pytest wrote its post-session marker. Rerun the focused profile once. Evidence
under `.artifacts/verify-omnigent/` is intentionally preserved.

## Optional Databricks compatibility

The Universe lane is explicit opt-in. Local profiles do not discover or execute
it, and record `universe=not_requested`. Credentialed CLI REPL, live harness
turns, and Electron signing/notarization are also opt-in. `auto` and
`all-surfaces` list them as `not_requested` with exact invocation guidance; they
never call them unless passed `--with-universe`. Use `--oss-ref <commit>` when
the requested commit is not the current checkout:

```bash
.agents/skills/verify-omnigent/scripts/verify.sh auto \
  --base-ref origin/main --with-universe --oss-ref <commit>
```

Follow [compatibility.md](compatibility.md). Do not pin a Universe branch or
assume its Python and UI vendor refs are equal.

Local one-shot verification does not prove Windows-native packaging, real
mobile software keyboards, credentialed provider or SSO flows,
signing/notarization, managed Universe runtime on Linux, or production
EStore/MySQL latency. Report these as `not run` unless the corresponding
environment-specific proof was actually executed.

The current Universe preflight still invokes Bazel in the selected live
Universe checkout. Git state is checked, but ignored Bazel outputs are not fully
isolated. A disposable Universe worktree/output-base redesign remains deferred;
do not describe it as locally proved.

## Helper

`scripts/verify.sh` is the executable entry point:

```bash
.agents/skills/verify-omnigent/scripts/verify.sh \
  <doctor|auto|all-surfaces|quality-gates|universe|server|backend|cli|web-ui|desktop|harness-client|harness-live|perf|db-migration-deploy|smoke|collaboration|automations|core|full-ui> \
  [doctor-profile] [--base-ref REF] [--oss-ref REF] [--with-universe]
```

Cursor and Claude Code use thin project discovery adapters that point here.
Codex launched by Omnigent only discovers bundle skills and host skills copied
into its private `CODEX_HOME`; it does not scan this checkout's `.agents/skills`
directory. Install the canonical directory into the host source, or copy it
into an agent bundle:

```bash
.agents/skills/verify-omnigent/scripts/install-codex.sh --host
.agents/skills/verify-omnigent/scripts/install-codex.sh --bundle ./path/to/agent
```

The host mode creates a symlink and refuses to overwrite an existing skill.
The bundle mode copies the canonical skill at install time so uploaded bundles
are self-contained; there is still one tracked canonical source. Installed
scripts target the Git worktree from which they are invoked. When invocation is
outside that worktree, set `OMNIGENT_VERIFY_REPO_ROOT` explicitly; installed
paths are never treated as the product checkout.

Keep [features/README.md](features/README.md) and its linked feature files
current when routes, menus, capabilities, or supported deployment modes change.
