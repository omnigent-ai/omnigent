# Native Harness Plugin Interface (Modular Registry Proposal)

Status: draft
Supersedes nothing; extends `designs/harness-plugin-interface.md`.

## Problem

Omnigent supports **headless / SDK harnesses** as community plugins today (see
`designs/harness-plugin-interface.md`). A package like `omnigent-foo` declares a
`HarnessContribution` entry point, fills `harness_modules` / `aliases` /
`install_specs`, and core wires it in generically — because an SDK harness plugs
in as *pure data*: one import-path string per harness, dispatched through
`omnigent.runtime.harnesses._HARNESS_MODULES` and `runner/routing.py`.

**Native (terminal / TUI) harnesses are not pluggable.** A native harness wraps
a real vendor CLI (Claude Code, Codex, Cursor, Pi, Goose, …) in a tmux/PTY or
local-server session, tails its transcript, mirrors output back into Omnigent,
and mediates auth / permissions / resume / interrupt. Adding one today means
editing core in ~10 places. The registry *rejects* any community contribution
that sets `native_harnesses` or `native_agents`:

```python
# omnigent/harness_plugins.py:716
if contribution.native_harnesses or contribution.native_agents:
    return (
        f"community harness plugin {entry_point_name!r} registers native terminal "
        "metadata, but community native terminal harnesses are not supported yet"
    )
```

`designs/harness-plugin-interface.md` § "Native TUI Harnesses" already names the
blockers: *"the runner, chat-resume, CLI-command, interrupt/stop, and built-in
agent seeding paths are not pluggable."* This proposal turns that list into a
concrete plan.

## What is already pluggable

The **data model** is done. `NativeCodingAgent` is a frozen dataclass of stable
wire metadata, contributions carry a tuple of them, and everything downstream
reads them through registry accessors:

- `omnigent/harness_plugins.py` — `NativeCodingAgent`, `HarnessContribution`
  (fields `native_harnesses`, `native_agents`), `native_agents()`,
  `native_harnesses()`.
- `omnigent/native_coding_agents.py` — indexes the registry rows by
  `agent_name` / `harness` / `wrapper_label` / `terminal_name`.
- `omnigent/_wrapper_labels.py` — the canonical wrapper-label string constants.
- `omnigent/harness_aliases.py` — canonicalization (`native-pi` → `pi-native`).

Nothing in this proposal changes the *shape* of `NativeCodingAgent`; it adds a
behavior side-channel and rewrites the dispatch that currently ignores it.

## What is NOT pluggable — the coupling inventory

Every blocker is **imperative per-harness dispatch** that branches on
`harness_name == "<x>-native"` or `native_agent.key == "<x>"` and does an inline
`import omnigent.<x>_native`. Grouped by hub:

### 1. The runner — `omnigent/runner/app.py` (~10.1k lines) + `omnigent/runner/native/orchestration.py` (~6.5k) — the epicenter

Phase 0 (#3148) moved the native *builders and mirrors* out of `app.py` into
`omnigent/runner/native/orchestration.py` (re-exported through
`omnigent/runner/native/__init__.py`), shrinking `app.py` from ~20.1k to
~10.1k lines. The imperative per-harness *dispatch* still lives in `app.py`;
it now calls the imported builders instead of locally-defined ones. The
coupling left to untangle:

- **Spawn-env dispatch** (`app.py`, 11 arms): `if harness_name ==
  "<x>-native" and spawn_env is None: ... build_<x>_native_spawn_env`.
- **Launch dispatch** (`app.py`, 11 arms) → `_auto_create_<x>_terminal(...)`.
- **`_auto_create_<x>_terminal`** functions (11 of them) — now in
  `runner/native/orchestration.py`; each imports its own `<x>_native_bridge` /
  `<x>_native_forwarder` / `<x>_native_permissions` and wires the transcript
  forwarder + permission/usage/compaction mirrors, alongside the
  `_supervise_*_bridges` mirrors (`_supervise_cursor_native_bridges`,
  `_supervise_goose_native_bridges`, `_supervise_hermes_native_bridges`,
  `_supervise_qwen_native_bridges`). Still the dominant blocker — the split
  gave it a home but the `if key ==` dispatch that reaches it is unchanged.
- **Interrupt / stop dispatch** (`app.py`) → `_handle_<x>_native_interrupt` /
  `_handle_<x>_native_stop` closures (kept in `app.py`, not extracted).
- **Terminal-route dispatch** (`app.py`): `terminal_name == "<x>"` →
  `_auto_create_<x>_terminal`.
- Plus the 11 `*_NATIVE_TERMINAL_ROLE` imports and the cost-popup bridge-dir
  dispatch (both in `app.py`).

### 2. Native launch — `omnigent/cli.py` (~14.5k lines)

Each native TUI is a hand-written `@cli.command` (`claude`, `codex`, `opencode`,
`pi`, `cursor`, `kiro`, `goose`, `hermes`, `antigravity`, `qwen`, `kimi`), each
importing `from omnigent.<x>_native import run_<x>_native` and calling
`_reject_native_on_windows("<x>")` with a literal name. No registry indirection
generates these.

### 3. Resume / resume-redirect

- `omnigent/resume_dispatch.py:216` (`_dispatch_wrapper`) — the canonical
  11-branch `if native_agent.key == "<x>":` chain, each `import
  run_<x>_native`. Used by `omnigent resume`.
- `omnigent/chat.py:1057` (`_redirect_native_resume_if_needed`) — a parallel,
  partially-covered (6 of 11) resume-redirect keyed on `native_agent.key`, with
  hand-written `_run_<x>_native_resume_redirect` helpers.

### 4. Built-in `*-native-ui` agent seeding — `omnigent/server/app.py`

`_ensure_default_agents` calls 11 hardcoded `_ensure_default_<x>_agent(...)`,
each paired with a `_build_<x>_native_bundle()` that imports
`_materialize_<x>_agent_spec`. `omnigent/db/utils.py:builtin_agent_id` and
`omnigent/session_import/local.py` depend on the fixed built-in names.

### 5. Enumerations parallel to the registry (should *derive* from it)

- `omnigent/spec/_omnigent_compat.py:88` — `OMNIGENT_HARNESSES` /
  `OMNIGENT_HARNESS_ALIASES` frozensets re-list all native ids + `native-*`
  aliases.
- `omnigent/onboarding/harness_readiness.py` — per-family frozensets gating
  readiness/auth.
- `omnigent/onboarding/harness_install.py:219` — `_HARNESS_NAME_TO_KEY`.
- `omnigent/model_override.py` / `omnigent/model_catalog.py` — `*_FAMILY` /
  `_CURSOR_HARNESSES` frozensets.
- `omnigent/server/routes/sessions.py` — `_FORK_HISTORY_NATIVE_HARNESSES`,
  `_CURSOR_FORK_HISTORY_HARNESSES`, per-harness wrapper-label/model constants,
  and fork/switch gating.
- `omnigent/runner/resource_registry.py` — 11 `*_NATIVE_TERMINAL_ROLE`
  constants + the native-role status set.
- `omnigent/runtime/harnesses/__init__.py:36` — a **dead** `_HARNESS_MODULES`
  literal listing every `<x>-native` module (overwritten at `:152`). Delete.

### 6. The web mirror — `web/src/lib/`

`nativeCodingAgents.ts` duplicates all 11 rows + aliases; `forkHarness.ts`,
`AgentCard.tsx` (icon switch), and `sessionStop.ts` / `sessionCapabilities.ts` /
`codexPlanMode.ts` hardcode wrapper-label literals. Truly community-contributable
native harnesses need the web driven by `GET /v1/harnesses`, not literals.

## Design: a `NativeHarnessProvider` behavior seam

Mirror how SDK harnesses supply *one import path* (`harness_modules[id]`). A
native harness supplies a small set of import paths for the lifecycle hooks the
dispatch hubs currently hardcode. `NativeCodingAgent` stays a pure-data
identity row; behavior lives in a sibling provider resolved lazily (respecting
the plugin import rules — `get_contribution()` must stay import-light).

```python
# omnigent/harness_plugins.py (new)
@dataclass(frozen=True)
class NativeHarnessProvider:
    """Import paths for a native harness's lifecycle hooks.

    Every value is a dotted path resolved lazily at dispatch time, so
    get_contribution() never imports the runner/CLI/provider stack.
    """
    key: str                       # matches NativeCodingAgent.key
    run_native: str                # "...:run_<x>_native"  (CLI + resume launch)
    auto_create_terminal: str      # "...:auto_create_<x>_terminal"  (runner)
    spawn_env_builder: str | None = None   # "...:build_<x>_native_spawn_env"
    interrupt_handler: str | None = None   # "...:handle_<x>_native_interrupt"
    stop_handler: str | None = None        # "...:handle_<x>_native_stop"
    materialize_agent_spec: str | None = None  # "...:_materialize_<x>_agent_spec"
    bridge_dir: str | None = None          # "...:bridge_dir_for_session" (cost popup)
```

Add to `HarnessContribution`:

```python
    native_providers: tuple[NativeHarnessProvider, ...] = ()
```

And accessors in `omnigent/harness_plugins.py`:

```python
def native_providers() -> tuple[NativeHarnessProvider, ...]: ...
def native_provider_for_key(key: str) -> NativeHarnessProvider | None: ...
```

A tiny resolver (new `omnigent/native_dispatch.py`) turns a dotted path into a
callable with `importlib`, caching per path, so each hub calls
`resolve(provider.run_native)(server=..., session_id=..., args=...)` instead of
an `if/elif` arm. `run_native` must accept a uniform `(*, server, session_id,
extra_args: tuple[str, ...])` signature — the per-harness `run_<x>_native`
functions are near-uniform already, so this is mostly a keyword-arg
normalization, not a rewrite.

### Signature normalization

The one real API change: today `run_claude_native(claude_args=...)`,
`run_pi_native(pi_args=...)` each name their pass-through arg differently. The
provider seam requires a single spelling (`extra_args`). Keep the existing
functions, add thin `**kwargs`-tolerant wrappers, or rename the parameter with a
back-compat alias for one release (per CLAUDE.md deprecation policy, note the
target release).

### Rewriting each hub

| Hub | Today | After |
|---|---|---|
| `resume_dispatch.py` `_dispatch_wrapper` | 11 `if key ==` arms | `resolve(provider.run_native)(...)` |
| `cli.py` native subcommands | 11 `@cli.command` funcs | loop over `native_agents()`, register one Click command each; `_reject_native_on_windows` reads the row |
| `runner/app.py` launch + terminal-route | 11 arms → `_auto_create_<x>_terminal` | `resolve(provider.auto_create_terminal)(...)` |
| `runner/app.py` spawn-env | 11 arms | `resolve(provider.spawn_env_builder)(...)` when set |
| `runner/app.py` interrupt/stop | 11 arms each | `resolve(provider.interrupt_handler / stop_handler)(...)` |
| `chat.py` resume-redirect | 6 arms | fold into the same provider `run_native`; delete the per-harness redirect helpers |
| `server/app.py` seeding | 11 `_ensure_default_<x>_agent` | loop over `native_agents()`, materialize via `provider.materialize_agent_spec` |
| enumerations (§5) | frozensets/dicts | derive from `native_agents()` / capability flags |

### Capability-driven behavior (replace the ad-hoc frozensets)

Several §5 sets encode *behavior*, not identity — e.g.
`_FORK_HISTORY_NATIVE_HARNESSES` ("rebuilds fork transcript") and
`_CURSOR_FORK_HISTORY_HARNESSES` ("replays history as a text preamble"). These
should become fields on `HarnessCapabilities` (which already exists and is
asserted in `tests/test_harness_capabilities.py`) — e.g. a `fork_history:
Literal["none","rebuild","preamble"]` axis — so the server reads the capability
instead of membership in a hand-maintained set. This also feeds `/v1/harnesses`
so the web can stop hardcoding `forkHarness.ts`.

### Validator flip

Once the hubs resolve through the registry, replace the hard reject in
`_validate_community_contribution` with positive validation:

- every `native_agent.key` has a matching `native_provider.key`;
- provider import paths start with `COMMUNITY_MODULE_PREFIX` (same rule as
  `harness_modules`);
- native-agent identity values don't collide with an existing contribution
  (the `_native_agent_identity_values` check already exists — keep it);
- `run_native` and `auto_create_terminal` are non-empty.

## Phasing

This is a **substantial refactor, not a small extension**. The realistic path
is an internal refactor first (built-in native harnesses keep living in core but
route through the generic seam), then a thin follow-up that opens it to
community packages.

### Phase 0 — Prep: split the oversized dispatch files

The refactor is concentrated in files that are already too large to edit safely.
The goal is **< 10k lines per file**. Before adding the seam, carve the
native-specific code into cohesive modules so the provider rewrite touches small
files with clear boundaries. This is behavior-preserving and independently
reviewable/mergeable. Each extraction is a mechanical move + import fix, verified
by the existing test suite and `pre-commit run --all-files`. No behavior change;
no `if key ==` arm removed yet.

Done:

- **`cli.py`** ✅ (#3047) — native subcommand bodies moved into
  `omnigent/cli_native.py` (they already delegate to `run_<x>_native`); `cli.py`
  registers them. `cli.py` is now 9.6k lines; `cli_native.py` 1.3k.
- **`server/routes/sessions.py`** ✅ (#3097) — split into a facade
  (`sessions.py`, now 7.8k) that star-imports an impl package
  (`omnigent/server/routes/_sessions/`: `common.py`, `helpers.py`,
  `orchestration.py`). `create_sessions_router` stays in the facade.
- **`runner/app.py`** ✅ (#3148) — the native builders and bridge mirrors
  (`_auto_create_*_terminal`, `_supervise_*_bridges`, the transcript-forwarder
  task registry, cost-popup repop tasks) moved into
  `omnigent/runner/native/orchestration.py` (~6.5k lines), re-exported through
  `omnigent/runner/native/__init__.py`; `app.py` imports them. `app.py` dropped
  from ~20.1k to ~10.1k lines. Landed as a single `orchestration.py` rather than
  the proposed `terminals.py` / `supervise.py` / `interrupt.py` three-way split —
  a further sub-split can happen when the seam lands if the module stays hot.
  The `if key ==` / `if harness_name ==` dispatch arms and the interrupt/stop
  handler closures stayed in `app.py` (they are the entry points Phase 1
  rewrites), so `app.py` is still marginally over the 10k target.
- **`tests/runner/test_app_sessions_native.py`** ✅ (#3149) — the ~19.0k-line
  monolith was split into nine concern-scoped modules
  (`test_app_sessions_native_{events_lifecycle,events_options,supervision,
  terminal_routing,terminals_autocreate,terminals_runtime,wake_forwarders,
  workflow_init,workflow_messages}.py`) plus a shared `tests/runner/conftest.py`
  (~0.7k) holding the scaffolding. Each new file is under 3k lines.

Deferred (under the 10k target already; fold into Phase 1 when the seam lands):

- **`chat.py`** (4.2k) → move the `_run_<x>_native_resume_redirect` helpers into
  `resume_dispatch.py` (they duplicate its dispatch anyway) as the first step of
  collapsing the two resume paths into one.

### Current state (verified 2026-07-24, at `main` `59e6b70e`)

Grounding the plan in the actual tree, not just the coupling inventory above:

- **Data model is ready.** `NativeCodingAgent` (`harness_plugins.py:49`) is 11
  frozen rows; `HarnessContribution` (`:70`) has `native_harnesses` /
  `native_agents` but **no** `native_providers` field yet;
  `native_coding_agents.py` already indexes rows by agent_name / harness /
  wrapper_label / terminal_name. `HarnessCapabilities`
  (`harness_capabilities.py:79`) exists with an optional-field extension
  pattern (`steering`, `live_queue`, `images`, `compaction`) but **no**
  `fork_history` axis.
- **`run_<x>_native` is already near-uniform.** All 11 are `(*, server,
  session_id, <x>_args, resume_picker=..., ...)`. The divergence is only the
  pass-through arg *name* plus four harnesses carrying extra kwargs: claude
  (`command`, `use_claude_config`), codex (`command`, `model`, `prompt`),
  antigravity (`command`, `model`, `permission_mode`), opencode (`model`). So
  signature normalization is a keyword-rename with a threaded `**extra`, not a
  rewrite — lower risk than "Signature uniformity" under Risks suggested.
- **Coverage is uneven across hubs** (a correctness smell the seam fixes):
  `resume_dispatch._dispatch_wrapper` covers 10, `chat.py`
  `_redirect_native_resume_if_needed` only 6 (missing opencode/goose/hermes/
  antigravity/qwen), runner interrupt handlers 9, stop handlers 7. Routing
  everything through one resolver *normalizes* coverage.
- **The dead `_HARNESS_MODULES` literal still exists** (`runtime/harnesses/
  __init__.py:36`, overwritten at `:152`) — not yet deleted.
- **`harness_catalog()` (`harness_plugins.py:899`) does not emit native-agent
  rows** — only `{id, label, capabilities?, setup_steps?}` per harness, no
  `agent_name` / `wrapper_label` / icon. The web is still 100% literals.

Roughly **60+ hardcoded duplication points across ~12 Python + 6 TS files**
plus the five dispatch hubs remain.

### Phase 1 — Internal provider seam (core-only)

Built-ins keep living in core but route through the generic seam. The test bar
for every PR here is **"every native harness behaves identically before/after"**
— lean on the split native test suite (#3149) and the native e2e skills. The
validator keeps rejecting community native metadata throughout Phase 1.

| PR | Scope | Key files | Depends on | Risk | Est. |
|---|---|---|---|---|---|
| **1.1 Provider model + resolver** | Add `NativeHarnessProvider` (import-path strings), the `native_providers` field + accessors, and `omnigent/native_dispatch.py` (lazy `importlib` resolver, cached per path). Populate 11 built-in providers pointing at existing `omnigent.<x>_native` functions. Purely additive — no hub rewired yet. | `harness_plugins.py`, new `native_dispatch.py` | — | Low | 1–2d |
| **1.2 Signature normalization** | Give `run_<x>_native` a uniform `extra_args` spelling with a back-compat `<x>_args` alias (one-release deprecation per CLAUDE.md — name the target release). Decide the `**extra` protocol for the four special-kwarg harnesses (claude/codex/antigravity/opencode). | 11 `omnigent/<x>_native.py`, `native_dispatch.py` | 1.1 | Low–Med (mechanical ×11) | 2–3d |
| **1.3 Resume hubs** | Collapse `resume_dispatch._dispatch_wrapper` (10 arms) and the 6 `chat.py` `_run_<x>_native_resume_redirect` helpers into one `resolve(provider.run_native)(...)` path. Deletes the redirect helpers and normalizes the 10-vs-6 coverage gap. | `resume_dispatch.py`, `chat.py` | 1.1, 1.2 | Med | 2d |
| **1.4 CLI subcommands** | Replace the 11 hand-written `@cli.command` funcs in `cli_native.py` with a loop over `native_agents()`, registering one Click command each; make `_reject_native_on_windows` a registry-driven guard. Wrinkle: per-command options (`--model`, `--command`) must come off provider/row metadata. | `cli_native.py`, `cli.py` | 1.1, 1.2 | Med | 2–3d |
| **1.5 Runner launch + terminal-route** | The epicenter. Replace spawn-env (22 arms), launch (11 + 3 elif), and terminal-route (11) dispatch in `app.py` with `resolve(provider.auto_create_terminal / spawn_env_builder)(...)`. **Preserve the `_supervise_*_bridges` forward-cursor / restart / double-post invariants exactly.** Likely splits into 1.5a spawn-env and 1.5b launch+route. | `runner/app.py`, `runner/native/orchestration.py` | 1.1, 1.2 | **High** | 4–6d |
| **1.6 Runner interrupt/stop** | Route interrupt/stop through `resolve(provider.interrupt_handler / stop_handler)`; fill the 9/7 coverage gaps so every native has both paths. | `runner/app.py` | 1.1 | Med | 2d |
| **1.7 Seeding loop** | Replace the 26 `_ensure_default_<x>_agent` / `_build_<x>_native_bundle` touchpoints in `server/app.py` with a loop materializing via `provider.materialize_agent_spec`. **`builtin_agent_id` output must stay byte-identical** so redeploy doesn't orphan seeded agents — pin this with a test. | `server/app.py`, `db/utils.py` | 1.1 | Med | 2–3d |
| **1.8 Derive enumerations** | Add a `fork_history: Literal["none","rebuild","preamble"]` axis to `HarnessCapabilities`; derive the §5 frozensets/dicts from `native_agents()` / capabilities (8 files, ~35 sets); delete the dead `_HARNESS_MODULES` literal. | `harness_capabilities.py`, `_omnigent_compat.py`, `harness_readiness.py`, `harness_install.py`, `model_override.py`, `model_catalog.py`, `_sessions/common.py`, `resource_registry.py`, `runtime/harnesses/__init__.py`, `tests/test_harness_capabilities.py` | 1.1 | Med | 2–3d |

After 1.1 + 1.2 land, PRs 1.3–1.8 touch mostly disjoint hubs and can proceed in
parallel. **Phase 1 subtotal: ~17–25 engineer-days.**

### Phase 2 — Open to community packages

Only starts once Phase 1 has every built-in running *through* the seam.

| PR | Scope | Key files | Depends on | Risk | Est. |
|---|---|---|---|---|---|
| **2.1 Validator flip** | Replace the hard reject in `_validate_community_contribution` with positive validation: every `native_agent.key` has a matching `native_provider.key`; provider import paths start with `COMMUNITY_MODULE_PREFIX`; identity values don't collide (`_native_agent_identity_values` already checks this); `run_native` + `auto_create_terminal` are non-empty. | `harness_plugins.py` | 1.1 | Low–Med | 1d |
| **2.2 `/v1/harnesses` native rows** | Extend `harness_catalog()` to emit native-agent rows + capabilities (`agent_name`, `wrapper_label`, `fork_history`, icon/label field), so the web has a server source of truth. | `harness_plugins.py`, `server/routes/harnesses.py` | 1.8 | Low | 2d |
| **2.3 Web off the endpoint** | Delete the `nativeCodingAgents.ts` literals + `HARNESS_ALIASES`, the `forkHarness.ts` sets (`NATIVE_REBUILD_HARNESSES` / `PREAMBLE_FORK_HARNESSES` now come from `fork_history`), the `AgentCard` icon switch, and the wrapper-label literals in `sessionStop.ts` / `sessionCapabilities.ts` / `codexPlanMode.ts` — all driven by `/v1/harnesses`. Needs a **demo (screenshots/recording)** per CLAUDE.md; likely splits into 2.3a fork/capabilities data-plumb and 2.3b icon/label rendering. | `web/src/lib/*`, `web/src/components/AgentCard.tsx` | 2.2 | Med–High (largest FE) | 4–6d |
| **2.4 Docs + example plugin** | Extend `designs/harness-plugin-interface.md` § "Native TUI Harnesses" with the native checklist, and ship an example native plugin (`examples/` or a sibling `omnigent-foo-native`) proving the contract end to end. | `designs/harness-plugin-interface.md`, `examples/` | 2.1, 2.2 | Low–Med | 2–3d |

**Phase 2 subtotal: ~9–12 engineer-days.**

### Effort summary

- **Phase 1** (internal seam): ~17–25 engineer-days.
- **Phase 2** (community + web): ~9–12 engineer-days.
- **Total: ~26–37 engineer-days** of focused work across ~12 PRs (splittable to
  ~14 with 1.5 and 2.3 breaking in two). Folding in review cycles, CI, and
  runner e2e validation, that is realistically **~2–3 calendar months** done
  alongside other work. The critical path is 1.1 → 1.2 → 1.5 (runner) →
  2.2 → 2.3 (web); the risk center is **PR 1.5**, where the `_supervise_*_bridges`
  invariants live.

## Risks and open questions

- **Runner extraction is the risk center.** The `_supervise_*_bridges` mirrors
  hold subtle forward-cursor / restart / double-post invariants (see the
  `_AUTO_FORWARDER_TASKS` transcript-forwarder registry, now in
  `runner/native/orchestration.py`). Phase 0's move (#3148) preserved these
  behaviorally — verified by the split native test suite (#3149) — so the
  remaining risk shifts to Phase 1, where the dispatch that reaches these
  mirrors gets rewritten. Lean on the existing native e2e skills
  (`claude-native-ui:build-omnigent`, `pi-native-e2e-dev`, etc.).
- **Signature uniformity.** Not every native launcher is trivially uniform
  (opencode has a cold-boot app-server path, codex has WS JSON-RPC). The
  provider may need an optional `transport`/`cold_boot` hook rather than
  forcing one signature. Validate against the two hardest (codex, opencode)
  before committing the protocol.
- **Windows.** `_reject_native_on_windows` must keep firing for contributed
  natives — make it a registry-driven guard, not per-command.
- **Import hygiene.** Providers hold *strings*; the resolver is the only place
  that imports harness modules, and only at dispatch time — preserving the
  plugin import rules from `harness-plugin-interface.md`.
- **Capability axis scope.** Which of the §5 sets are genuinely
  behavior-capabilities (belong on `HarnessCapabilities`) vs. pure identity
  (derive from rows) needs a per-set decision; fork-history is the clearest
  capability candidate.

## Bottom line

The data model is ready and Phase 0 (the file splits) has landed. The remaining
work is untangling native orchestration from five `runner/app.py` chains and
four other hubs into a `NativeHarnessProvider` behavior seam, then flipping the
validator — sequenced as ~12 PRs (Phase 1: 1.1–1.8 core-only; Phase 2: 2.1–2.4
community + web), ~26–37 engineer-days total. Start with the additive foundation
(1.1 provider model + resolver), which unblocks everything; the risk center is
1.5 (runner launch/terminal-route), where the `_supervise_*_bridges` invariants
live.
