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

### 1. The runner — `omnigent/runner/app.py` (~19.7k lines) — the epicenter

Five separate 11-branch chains plus their handlers:

- **Spawn-env dispatch** (`~:8887`, again `~:14124`): `if harness_name ==
  "<x>-native": from omnigent.<x>_native_bridge import build_<x>_native_spawn_env`.
- **Launch dispatch** (`~:9015`): 11 branches → `_auto_create_<x>_terminal(...)`.
- **`_auto_create_<x>_terminal`** functions (11 of them) — each imports its own
  `<x>_native_bridge` / `<x>_native_forwarder` / `<x>_native_permissions` and
  wires the transcript forwarder + permission/usage/compaction mirrors. This is
  the dominant blocker: e.g. `_supervise_cursor_native_bridges`,
  `_supervise_goose_native_bridges`, `_supervise_hermes_native_bridges`,
  `_supervise_qwen_native_bridges`.
- **Interrupt dispatch** (`~:15169`) → `_handle_<x>_native_interrupt`.
- **Stop dispatch** (`~:15262`) → `_handle_<x>_native_stop`.
- **Terminal-route dispatch** (`~:15840`): `terminal_name == "<x>"` →
  `_auto_create_<x>_terminal`.
- Plus the 11 `*_NATIVE_TERMINAL_ROLE` imports (`~:60`) and the cost-popup
  bridge-dir dispatch (`~:12779`).

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

The refactor is concentrated in files that are already too large to edit safely
(`sessions.py` 22.6k lines, `runner/app.py` 19.7k, `cli.py` 14.5k). Before
adding the seam, carve the native-specific code into cohesive modules so the
provider rewrite touches small files with clear boundaries. This is behavior
-preserving and independently reviewable/mergeable.

- **`runner/app.py`** → extract native orchestration into
  `omnigent/runner/native/` (e.g. `terminals.py` for the `_auto_create_*`
  builders, `supervise.py` for the `_supervise_*_bridges` mirrors,
  `interrupt.py` for interrupt/stop handlers). `app.py` keeps the (soon-to-be
  registry-driven) dispatch entry points and imports from the new package.
- **`cli.py`** → move the native subcommand bodies into
  `omnigent/cli_native.py` (they already delegate to `run_<x>_native`), leaving
  `cli.py` to register them.
- **`server/routes/sessions.py`** → extract native fork/switch/status gating
  into `omnigent/server/routes/_native_sessions.py`.
- **`chat.py`** → move the `_run_<x>_native_resume_redirect` helpers into
  `resume_dispatch.py` (they duplicate its dispatch anyway) as the first step of
  collapsing the two resume paths into one.

Each extraction is a mechanical move + import fix, verified by the existing test
suite and `pre-commit run --all-files`. No behavior change; no `if key ==` arm
removed yet.

### Phase 1 — Internal provider seam (core-only)

1. Add `NativeHarnessProvider`, `native_providers` field, accessors, and
   `omnigent/native_dispatch.py` resolver.
2. Populate the built-in contribution with one provider per native agent,
   pointing at the existing `omnigent.<x>_native` functions.
3. Normalize `run_<x>_native` to the uniform keyword signature (with aliases).
4. Rewrite each hub (table above) to resolve through the registry. Delete the
   `if key ==` chains and the dead `_HARNESS_MODULES` literal.
5. Derive the §5 enumerations from `native_agents()` / capabilities.
6. Keep the validator rejecting community native metadata — nothing external
   yet. All existing native harnesses now run *through* the seam. This is the
   correctness-critical phase; the test bar is "every native harness behaves
   identically before/after."

### Phase 2 — Open to community packages

1. Flip `_validate_community_contribution` to positive validation.
2. Extend `GET /v1/harnesses` (`harness_catalog()`) to emit native-agent rows +
   capabilities.
3. Drive the web off `/v1/harnesses`: delete the `nativeCodingAgents.ts`
   literals, `forkHarness.ts`, and the `AgentCard` icon switch in favor of
   server-supplied metadata (icon can be a capability/label field).
4. Document the native checklist in `designs/harness-plugin-interface.md`
   (extend § "Native TUI Harnesses").
5. Ship an example native plugin (`examples/` or a sibling `omnigent-foo-native`)
   to prove the contract end to end.

## Risks and open questions

- **Runner extraction is the risk center.** The `_supervise_*_bridges` mirrors
  hold subtle forward-cursor / restart / double-post invariants (see the
  transcript-forwarder registry at `runner/app.py:302`). Phase 0 must preserve
  these exactly; lean on the existing native e2e skills
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

The data model is ready; the work is untangling native orchestration from five
`runner/app.py` chains and four other hubs into a `NativeHarnessProvider`
behavior seam, then flipping the validator. Do the file split (Phase 0) first so
the seam lands in small, reviewable modules, then the core-only seam (Phase 1),
then community enablement (Phase 2).
