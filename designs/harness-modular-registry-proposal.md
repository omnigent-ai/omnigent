# Native Harness Plugin Interface (Modular Registry Proposal)

Status: Phase 1 complete (2026-07-31); Phase 2 (community + web) pending.
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
| **1.5a Runner spawn-env** | Collapse the spawn-env dispatch — **two** near-identical 11-arm blocks (`app.py` ~2720 and ~6236) — behind a uniform `build_spawn_env(session_id, *, server_client, labels)` provider hook. Absorbs the three shapes (bare / bridge-id-from-labels / hermes policy-hook write) behind that one signature. Self-contained; removes the duplication. The bounded first measurement of the runner. | `runner/app.py`, 11 `<x>_native_bridge.py` | 1.1, 1.2 | Med | 2–3d |
| **1.5b Runner launch** | The epicenter. The launch arms (`app.py` ~2841–3316 + the ~6095 elif chain) do **not** share a signature — `_auto_create_<x>_terminal` has 11 divergent signatures (3 common params; claude carries 9 extras). Unify by passing a `NativeLaunchContext` dataclass to a uniform `provider.auto_create_terminal(ctx)` adapter, with explicit `pre_launch` hooks for the non-uniform arms (claude transfer-inbound + rebuild-on-switch, codex needs-terminal check, antigravity host-spawn + transfer, opencode cold-boot terminal-ensure on the turn path ~6329). **Preserve the `_supervise_*_bridges` forward-cursor / double-post invariants exactly.** | `runner/app.py`, `runner/native/orchestration.py` | 1.5a | **High** | 4–6d |
| **1.5c Runner terminal-route** | Collapse the `terminal/attach` `ensure_native_terminal` dispatch (`app.py` ~7462–7908). 9/11 are a uniform check→create→return; codex + antigravity need an ownership-check + response-wrap hook (`_is_runner_owned_*`, `_codex_ensure_response_with_policy_notice`). Reuses 1.5b's context object. | `runner/app.py` | 1.5b | Med | 2d |
| **1.6 Runner interrupt/stop** | Route interrupt/stop through the provider; fill the 9-interrupt / 7-stop coverage gaps so every native has both paths. **Not additive as first scoped:** every handler is a closure over app-scope state (`server_client`, `resource_registry`, `_publish_event`, the `_AUTO_FORWARDER_TASKS` / `_session_*` module dicts), so extracting to module-level provider functions requires threading those in as a dependency-injection context, not a plain move. | `runner/app.py`, `runner/native/orchestration.py` | 1.5b | Med–High | 3–4d |
| **1.7 Seeding loop** | Replace the 26 `_ensure_default_<x>_agent` / `_build_<x>_native_bundle` touchpoints in `server/app.py` with a loop materializing via `provider.materialize_agent_spec`. **`builtin_agent_id` output must stay byte-identical** so redeploy doesn't orphan seeded agents — pin this with a test. | `server/app.py`, `db/utils.py` | 1.1 | Med | 2–3d |
| **1.8 Derive enumerations** | Add a `fork_history: Literal["none","rebuild","preamble"]` axis to `HarnessCapabilities`; derive the §5 frozensets/dicts from `native_agents()` / capabilities (8 files, ~35 sets); delete the dead `_HARNESS_MODULES` literal. Also add optional `shell_tool_name` / `shell_tool_prompt` fields so the harness bench's tool-call probe can be driven off capabilities instead of its hardcoded `_NATIVE_TOOL_PROVOCATION` table (see "Harness bench compatibility" below). | `harness_capabilities.py`, `_omnigent_compat.py`, `harness_readiness.py`, `harness_install.py`, `model_override.py`, `model_catalog.py`, `_sessions/common.py`, `resource_registry.py`, `runtime/harnesses/__init__.py`, `tests/harness_bench/native_tui_driver.py`, `tests/test_harness_capabilities.py` | 1.1 | Med | 2–3d |

After 1.1 + 1.2 land, PRs 1.3, 1.4, 1.7, 1.8 touch mostly disjoint hubs and can
proceed in parallel. The runner sub-stack (1.5a → 1.5b → 1.5c → 1.6) is serial —
each reuses the prior's context object — and is the critical path.
**Phase 1 subtotal (as estimated): ~20–29 engineer-days.** **Actual: all 10 PRs
landed (1.4 descoped, 1.5b split into i/ii); see the Implementation progress
ledger and the Phase 1 retrospective in Calibration below.**

### Phase 2 — Open to community packages

Only starts once Phase 1 has every built-in running *through* the seam.

| PR | Scope | Key files | Depends on | Risk | Est. |
|---|---|---|---|---|---|
| **2.1 Validator flip** | Replace the hard reject in `_validate_community_contribution` with positive validation: every `native_agent.key` has a matching `native_provider.key`; provider import paths start with `COMMUNITY_MODULE_PREFIX`; identity values don't collide (`_native_agent_identity_values` already checks this); `run_native` + `auto_create_terminal` are non-empty. | `harness_plugins.py` | 1.1 | Low–Med | 1d |
| **2.2 `/v1/harnesses` native rows** | Extend `harness_catalog()` to emit native-agent rows + capabilities (`agent_name`, `wrapper_label`, `fork_history`, icon/label field), so the web has a server source of truth. | `harness_plugins.py`, `server/routes/harnesses.py` | 1.8 | Low | 2d |
| **2.3 Web off the endpoint** | Delete the `nativeCodingAgents.ts` literals + `HARNESS_ALIASES`, the `forkHarness.ts` sets (`NATIVE_REBUILD_HARNESSES` / `PREAMBLE_FORK_HARNESSES` now come from `fork_history`), the `AgentCard` icon switch, and the wrapper-label literals in `sessionStop.ts` / `sessionCapabilities.ts` / `codexPlanMode.ts` — all driven by `/v1/harnesses`. Needs a **demo (screenshots/recording)** per CLAUDE.md; likely splits into 2.3a fork/capabilities data-plumb and 2.3b icon/label rendering. | `web/src/lib/*`, `web/src/components/AgentCard.tsx` | 2.2 | Med–High (largest FE) | 4–6d |
| **2.4 Docs + example plugin** | Extend `designs/harness-plugin-interface.md` § "Native TUI Harnesses" with the native checklist, and ship an example native plugin (`examples/` or a sibling `omnigent-foo-native`) proving the contract end to end. **Acceptance criterion: the example plugin is benchable** — `python -m tests.harness_bench --harness <plugin> --live` runs green (selection + native-tui driver + provisioning), which is the honest end-to-end proof the contract holds. | `designs/harness-plugin-interface.md`, `examples/` | 2.1, 2.2 | Low–Med | 2–3d |

**Phase 2 subtotal: ~9–12 engineer-days.**

### Harness bench compatibility

Does the harness bench (`tests/harness_bench/`, per
`designs/harness-capabilities-bench-seam.md`) work with a community-contributed
native plugin after this refactor? **Mostly yes, with no separate bench-migration
PR — the bench's selection/driver layer is already registry-driven.** Verified
against the current tree:

- **Selection is already registry-driven.** `manifest.py` seeds
  `OFFICIAL_PROFILES` from the 4 P0 SDK probes, then loops over
  `harness_capabilities()` adding every harness whose `integration_mode is
  NATIVE_TUI` (`_native_tui_harnesses()`), auto-generating a `BenchProfile` per
  native via `_native_profile()`. Community harnesses also resolve through
  `_registry_profile()` (`harness_modules()` / `harness_capabilities()`), and the
  CLI accepts a `module:attr` `BenchProfile` reference. So a plugin declaring
  `native_agents` + a `NATIVE_TUI` capability **enumerates and gets a profile with
  zero bench edits**.
- **The driver is already generic.** `transport.py`'s `driver_registry()` keys on
  transport/integration-mode; a native profile auto-selects `NativeTuiDriver`,
  which spawns a real server + runner and drives the vendor TUI through the
  session API. No `if harness == "x"` in the driver path.
- **Gap 1 — provisioning needs registry-driven seeding, which is PR 1.7.** The
  native driver provisions a session against a pre-seeded `<harness>-ui` agent
  (`native_tui_driver.py` → `_agent_id(vendor.agent_name)`). Today that agent
  exists only because `server/app.py` hardcodes its seeding, so a community
  plugin's agent isn't on the server and the run fails at provisioning. **PR 1.7
  (registry-driven seeding loop) closes this for free** — no separate bench work.
- **Gap 2 — tool-call probe metadata, folded into PR 1.8.** The tool/MCP probe
  reads a hardcoded `_NATIVE_TOOL_PROVOCATION` table (the per-vendor "run this
  shell tool" prompt); a plugin can't supply it, so those probes *skip*
  (NOT_APPLICABLE — non-fatal; basic-turn / streaming / interrupt / reasoning
  probes still run). The `shell_tool_name` / `shell_tool_prompt` capability fields
  added in **1.8** let the bench read this off the registry instead.

Net: **no new phase or standalone bench-migration PR.** Full provisioning falls
out of 1.7; tool-call probes become plugin-drivable via a small 1.8 field; and
2.4's example plugin carries a `--live` bench run as its acceptance criterion.

### Effort summary

- **Phase 1** (internal seam): **DONE.** Estimated ~20–29 engineer-days across
  10 PRs; actually shipped as 10 PRs (1.4 descoped, 1.5b split into i/ii) merged
  over 4 calendar days (07-28 → 07-31) as a focused stack. See the Phase 1
  retrospective in Calibration.
- **Phase 2** (community + web): ~9–12 engineer-days across 4 PRs — **remaining
  work.** 2.1 (validator flip) depends only on 1.1 and can start immediately;
  2.2 depends on 1.8's capability catalog (landed); 2.3 (web) is the largest FE
  piece and the new risk center; 2.4 ships the example plugin + docs.
- **Critical path remaining: 2.1 → 2.2 → 2.3 (web), with 2.4 alongside.** The
  runner sub-stack that was the original risk center is behind us with no
  invariant regressions.

### Calibration — Phase 1 retrospective (updated 2026-07-31, all of Phase 1 landed)

Estimate vs. actual, grounded in the shipped ledger:

- **Estimate: ~20–29 engineer-days across 10 PRs. Actual: 10 PRs merged in 4
  calendar days (07-28 → 07-31).** The engineer-day estimate assumed the work
  ran alongside other duties with normal review latency; run as a focused
  stack it compressed hard. The *shape* of the estimate held (relative sizing
  was right); the calendar framing (~2.5–3.5 months) did not survive contact
  with a dedicated push.
- **Additive/mechanical PRs came in under estimate — confirmed.** 1.1, 1.3,
  1.5a landed fast; 1.1 rode the pre-built `load_object` resolver. The
  prediction that "1.4, 2.1, 2.4 likewise come in low" is untested for 2.x
  (1.4 was descoped).
- **The real cost was test-shape churn + review-caught behavior bugs, not the
  seam itself.** Every substantial PR's time went into the tests that pin exact
  call shapes and into behavior-preservation defects caught in review, not the
  mechanical collapse:
  - 1.2 shipped a **commit-boundary defect** (test edits its production code
    needed were committed in 1.3) — surfaced only because 1.2 was opened
    stacked/isolated.
  - 1.5b-ii's first cut had a **claude force-recreate/transfer-inbound
    ordering bug** (short-circuited before the inbound check) — caught by
    review, fixed with a dedicated test.
  - 1.7 dropped built-in agent-name constants and let tests fall to magic-string
    literals; restored as a shared public block after review. It also missed the
    opencode e2e in the first sweep (follow-up #3656).
  - 1.8's first derivation used canonical-only fork-history ids and **regressed
    the reversed-`native-*`-spelling gating** (`native-claude` etc. don't
    canonicalize) — caught by an existing test in the broad run, fixed to emit
    both spellings.
- **The runner was correctly re-scoped, and the split paid off.** Reading the
  code (not guessing) drove 1.5 → 1.5a/b/c and 1.5b → 1.5b-i/ii, isolating the
  8 uniform launch arms from the 3 special ones. 1.6's DI extraction
  (`NativeInterruptRunner`, mirroring `CodexGoalRunner`) matched the prediction
  that interrupt/stop needed a dependency-injection context, not a plain move.
  The `_supervise_*_bridges` invariants were preserved with no incident.
- **Recurring operational friction (not code):** the local `uv` shell wrapper
  rewrites `uv.lock` registry URLs to an internal proxy on every `uv run` —
  guarded on every commit with `command uv run --no-sync` + `git restore
  uv.lock`. And a class of **full-suite-only test failures** (a shared
  model-catalog cache polluting `test_sessions_snapshot`; a locally-installed
  `qwen-openclaw-test` plugin) reproduce on clean `main` and pass in isolation —
  environmental, not regressions, but they cost verification time each PR.
- **Lesson for Phase 2:** the estimate-vs-actual gap was almost entirely
  calendar-vs-effort framing, not sizing error. Keep sizing the PRs the same
  way; stop projecting calendar months for a focused stack.

### Implementation progress

Ledger of what actually shipped — the plan tables above stay the stable target.
**Phase 1 is complete: 10 PRs landed 2026-07-28 → 07-31.** 1.4 (CLI) was
descoped during execution (see note below); 1.5 split into 1.5a/b/c and 1.5b
into 1.5b-i/ii once the runner exploration showed the launch arms didn't share a
signature.

| PR | Status | Link | Merged |
|---|---|---|---|
| 1.1 Provider model + resolver | landed | #3239 | 07-28 |
| 1.2 Signature normalization | landed | #3244 | 07-29 |
| 1.3 Resume hubs | landed | #3314 | 07-29 |
| 1.4 CLI subcommands | **descoped** | — | — |
| 1.5a Runner spawn-env | landed | #3495 | 07-29 |
| 1.5b-i Runner launch (scaffolding + 8 uniform arms) | landed | #3500 | 07-29 |
| 1.5b-ii Runner launch (3 special arms + turn-path opencode) | landed | #3501 | 07-30 |
| 1.5c Runner terminal-ensure (attach path) | landed | #3543 | 07-30 |
| 1.6 Runner interrupt/stop (migration; gap-fill deferred) | landed | #3568 | 07-31 |
| 1.7 Server seeding loop | landed | #3599 | 07-31 |
| 1.7 follow-up: opencode e2e onto shared agent-name constant | landed | #3656 | 07-31 |
| 1.8 Derive enumerations + fork_history/shell-tool capability axes | landed | #3648 | 07-31 |

**1.4 descoped.** The CLI-subcommand collapse (`cli_native.py` → a loop over
`native_agents()`) is orthogonal to making native harnesses community-pluggable:
the 11 hand-written `@cli.command` funcs are a *local* readability win, not a
dispatch hub the seam must own, and nothing in Phase 2 depends on it. Deferred as
optional cleanup rather than run for its own sake. `_reject_native_on_windows`
staying per-command is captured under Risks.

**Two behavior-preservation deltas landed as intended, called out here for the
record:** (1) the qwen-native error label shifted lowercase `"qwen"` →
`"Qwen Code"` (the agent's `display_name`) in the 1.5b/c runner collapse —
cosmetic; (2) the antigravity attach path now starts the MCP comment relay (the
shared launch context wires it for all harnesses), a benign superset the builder
already guards. Both are pinned by tests and were surfaced in the respective PRs.

## Risks and open questions

- **Runner extraction is the risk center.** The `_supervise_*_bridges` mirrors
  hold subtle forward-cursor / restart / double-post invariants (see the
  `_AUTO_FORWARDER_TASKS` transcript-forwarder registry, now in
  `runner/native/orchestration.py`). Phase 0's move (#3148) preserved these
  behaviorally — verified by the split native test suite (#3149) — so the
  remaining risk shifts to Phase 1, where the dispatch that reaches these
  mirrors gets rewritten. Lean on the existing native e2e skills
  (`claude-native-ui:build-omnigent`, `pi-native-e2e-dev`, etc.).
- **Signature uniformity — confirmed non-uniform (runner exploration).** The
  `run_<x>_native` *launchers* normalized cleanly (1.2). The *runner* builders
  did not: `_auto_create_<x>_terminal` has 11 divergent signatures (3 common
  params; claude carries 9 extras — `bundle_dir`, `skills_filter`,
  `auth_token_factory`, `resolve_launch_config`, …), and the launch arms carry
  irreducible per-harness pre-call work (claude transfer/rebuild, codex/
  antigravity needs+transfer checks, opencode turn-path cold-boot). The seam
  therefore passes a `NativeLaunchContext` dataclass to a uniform
  `provider.auto_create_terminal(ctx)` adapter with explicit `pre_launch` hooks —
  not a single uniform positional call. Interrupt/stop handlers close over
  app-scope state and need a DI context, not a plain extraction. This is why 1.5
  became 1.5a/b/c and 1.6 was re-scoped upward.
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

**Phase 1 is complete (2026-07-31).** Every built-in native harness now runs
*through* the `NativeHarnessProvider` seam: launch, terminal-route, interrupt/stop,
spawn-env, resume, built-in seeding, and the capability-derived enumerations are
all registry-driven, with 1.4 (CLI) descoped as optional cleanup. The runner
sub-stack — the original risk center — landed with its `_supervise_*_bridges`
forward-cursor / double-post invariants intact and no behavior regressions beyond
two intentional, test-pinned deltas (qwen error label, antigravity relay).

**Remaining work is Phase 2 — the actual payoff:** flip
`_validate_community_contribution` to accept native contributions (2.1), expose
native rows + capabilities on `/v1/harnesses` (2.2), drive the web off that
endpoint instead of the `web/src/lib` literals (2.3, the largest front-end piece
and new risk center), and ship a benchable example native plugin + docs (2.4).
2.1 is unblocked and can start now; 2.2 needs 1.8's catalog (landed). The
estimate-vs-actual lesson from Phase 1: the sizing was right, the calendar-month
framing was not — size Phase 2 PRs the same way, don't project months for a
focused stack.
