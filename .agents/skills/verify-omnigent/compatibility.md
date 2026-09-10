# Databricks downstream compatibility lane

Run this lane only when the user explicitly opts in and an OSS change can affect
the vendored deployment in a sibling Universe checkout. Universe is a moving
consumer, not the OSS source of truth. Read its current
`agentbricks/mas/CLAUDE.md`, `agentbricks/mas/third_party/sync/README.md`, and
pinned refs before checking it.

## Compatibility matrix

| OSS change | Downstream risk | Required evidence |
|---|---|---|
| API request/response, event, route, or capability | Vendored UI, server, and Lakebox clients advance independently | Old client/new server and new client/old server behavior; absent fields default safely; additive wire contract |
| Store ABC or entity | Databricks, dual, and MySQL implementations can import but fail at runtime | Full MAS Python suite, including discovered store signature/abstract-method guards |
| DB model or Alembic revision | Databricks MySQL DDL is owned by USM and deploys separately; the service runtime must never run Alembic | OSS Alembic remains valid; matching USM forward/rollback migration; USM-before-code rollout; old server remains usable after expansion |
| Auth or permissions | Databricks resolves Barnacle request identity and stores ACLs in WHS, not OSS account or permission tables | Capability and server enforcement agree; anti-enumeration; owner/admin semantics; WHS UUID conversion and workspace-group behavior |
| Sharing | Some accounts cap sharing at `restricted_read_only`; workspace-wide access may be disabled | `on`, `read_only`, `restricted_read_only`, and `off`; hidden/disabled UI matches rejected grants |
| Automations | Managed MAS intentionally has no scheduler/store/router | `/v1/info.automations_enabled=false`; UI has no dead Automations route or calls |
| Projects/other optional feature | Deployment may expose a structural capability only when its router/store exists | Capability false with old server; true only when endpoint answers |
| Web dependency, host config, routing, or CSS | Universe vendors a separately pinned embeddable UI with host-owned fetch/WebSocket/router seams | Standalone build and embed build; host seam unchanged or backward-compatible; no second React/router; CSS remains scoped |
| Runner/host protocol | Lakebox wheels are versioned separately from vendored server source | Current and supported older runner against new server; new runner against supported older server where applicable |
| Dual-store behavior | Databricks can route sessions between EStore and MySQL | `mysqlEnabled` hides/shows the MySQL home correctly; `estoreReadOnly` rejects writes with usable migration/fork UX; cross-store forks retain the correct home |

## Runtime discovery

Use the read-only preflight from the OSS root:

```bash
.agents/skills/verify-omnigent/scripts/verify.sh universe \
  --base-ref <PR-base> --oss-ref <commit>
```

It uses `UNIVERSE_ROOT` when set and otherwise checks the sibling `../universe`.
It reads `UPSTREAM_REF` and `UI_UPSTREAM_REF` independently. Never change
branches, sync vendors, regenerate patches, or deploy as part of verification.

Python changes require `UPSTREAM_REF`, web/UI changes require
`UI_UPSTREAM_REF`, and mixed changes require both pins to match the requested
commit. A required mismatch records `not_synced` and exits nonzero. Clean
application of affected local patches is source compatibility only. Runtime
compatibility remains unproved until Universe vendors the requested commit and
the mapped runtime targets pass.

On Darwin, the lane runs checks that can produce valid test XML and records the
remaining targets as blocked. MAS runtime targets can fail before test execution
while compiling jemalloc against Linux `features.h`; the managed-host edge patch
gate can fail during collection when psutil cannot import `_psutil_osx`. Run the
blocked targets through the Linux/Bazel lane. Do not count a Bazel zero exit as
proof unless the target's `test.xml` exists, parses, and records at least one
test with no failures or errors.

## Read-only/source checks

Inspect the OSS diff against all three release axes: Python server/source,
separately pinned embedded UI, and Lakebox runner/host wheels. For every changed
public interface, check:

- `third_party/omnigent*` consumers and local patches;
- `python/omnigent_server` Databricks glue;
- the host config mirror in the embedded UI integration;
- USM MySQL migrations under `agentbricks/mas/db/omnigent/migrations`;
- Lakebox wheel/version consumers when runner behavior changes.

The Python and UI refs are deliberately independent, and Lakebox wheels are a
third timeline. A green same-revision test does not prove staggered rollout
safety.

Automatic source mapping is explicit: `omnigent/**` and
`sdks/python-client/**` select the Python pin; `web/**` and `sdks/ui/**` select
the UI pin. Other paths do not gain an implicit Universe compatibility claim.

`sync.sh sync --dry-run` lists intended patches but does not apply them. The
preflight archives the requested OSS commit into a temporary directory and runs
`git apply --check` there for each affected patch. It removes the temporary tree
afterward and verifies that the Universe checkout's git status did not change.

The current Bazel phase still runs in the selected live Universe checkout.
Verification requires both checkouts to have no tracked or non-ignored
untracked changes. Ignored generated outputs remain an explicit isolation
limitation, but stable untracked inputs are never silently omitted from pin and
patch selection.
Git-status checks do not account for every ignored Bazel output or convenience
symlink. Until a disposable Universe worktree with a temporary output base and
symlink prefix is implemented, report this as an isolation limitation rather
than a read-only filesystem proof.

## Universe verification commands

Run from the Universe root after it is stable:

```bash
agentbricks/mas/third_party/sync/sync.sh check
bazel test //agentbricks/mas/python:all \
  --tool_tag=ai-agent --test_output=errors \
  --noshow_progress --noshow_loading_progress
bazel build //agentbricks/mas/third_party/omnigent_ui:embed \
  --tool_tag=ai-agent --noshow_progress --noshow_loading_progress
bazel test //agentbricks/mas/third_party/omnigent_ui:bazel_lint_test \
  --tool_tag=ai-agent --test_output=errors \
  --noshow_progress --noshow_loading_progress
```

Do not run Bazel commands concurrently. Inspect `test.xml` for failures.

For a DB change, additionally prove that schema rollout can precede binary
rollout. Databricks USM owns DDL; the pod runtime must not invoke Alembic or
`create_all`. Add matching USM forward and rollback migrations, run the required
USM suites, and prove the old binary tolerates the expanded schema. The new
binary may rely on a new column only after USM has completed. Contract/removal
must be a later release.

For auth and permissions, do not emulate OSS password/header account tables.
Databricks resolves the caller from request context and maps session ACLs through
WHS. Verify hex UUID16 MySQL session IDs map to WHS IDs, `can_approve` degrades
safely where WHS cannot represent it, and workspace-wide access is a group
grant, not the OSS `__public__` sentinel.

For capability skew, probe the managed mount's `/v1/info`. Expected downstream
invariants include:

- `automations_enabled=false` and no calls to the 404ing scheduled-task routes;
- `projects_enabled=true` only when the MySQL project router/store is wired;
- sharing modes are SAFE-driven per request, and `public_sharing_enabled`
  fails closed;
- `/auth/users` and OSS Members administration are absent;
- `/api/2.0/omnigent` remains primary while the plural legacy route and stored
  `*.omnigentsession.json` names are preserved until coordinated migration.

SAFE config changes ship in a separate PR from service code.

For latency, OSS benchmark numbers are necessary but not sufficient. Check that
the embedded UI adds no eager fetch/poll when a capability is disabled, that
Databricks frontend poll intervals remain host-configurable, and that server
heartbeat/liveness/rescan intervals remain internally consistent. Measure
EStore/MySQL list fan-out with realistic data; an empty SQLite result cannot
detect quota-amplifying reads or production round-trip cost.

`verify.sh perf --base-ref <ref>` records a bounded local base/candidate
comparison only when host, harness, backend, seed, config, and journey identity
match. It includes p50/p99, total and per-operation request counts, routes, and
failures. It blocks when no explicit applicable p50 and p99 regression
thresholds were supplied. Even a matched SQLite result is not a production
EStore/MySQL claim.

For live managed-session verification, use LiteSwap and the current MAS
instructions. Never use hot reload/rsync while testing a managed turn: restarting
the pod drops the runner WebSocket tunnel and invalidates the result.

## Stop conditions

Do not call a change downstream-safe when:

- it requires server and UI to update atomically;
- a missing or unknown field crashes or enables an affordance;
- a new store method or parameter is absent downstream;
- a schema change has no separately deployable migration or rollback;
- WHS/restricted-sharing behavior was represented only by OSS header/account auth;
- EStore/MySQL dual-home behavior was tested against only one flag combination;
- a passing functional test is the only latency evidence.
