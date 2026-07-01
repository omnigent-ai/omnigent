# Modular, self-describing harnesses — design + migration plan

**Status:** Phase 0 implemented in `omnigent/harnesses/` (registry + capability model,
landing as stacked PRs); Phases 1-7 pending. Structural claims below re-verified against
`main` — the frontend has since moved `ap-web/` -> `web/` (reflected here); the per-harness
if-chains in `runner/app.py` (36), `resume_dispatch.py` (10), `cli.py` `_manage_*` (9) and
`server/app.py` `_ensure_default_*` (11) all still stand.
**Goal:** Adding or editing one coding-agent harness should touch ONLY that harness's
own files, and each harness should explicitly declare which features it supports.

---

## 1. Problem (recap, grounded in code)

### 1a. Shared-file merge treadmill
A new native harness today edits ~15 shared central files. Each edit is "add my entry
alongside everyone else's", so two harness PRs in flight always collide (Kiro #899 = 7
rounds; #1204 re-collided with kimi). The touch points, classified by **shape**:

| File | Per-harness contribution | Shape |
|---|---|---|
| `omnigent/runtime/harnesses/__init__.py` | `"name": "module.path"` in `_HARNESS_MODULES` | declarative dict |
| `omnigent/harness_aliases.py` | alias entry + `NATIVE_HARNESSES` member | declarative dict/set |
| `omnigent/native_coding_agents.py` | a `NativeCodingAgent(...)` instance + tuple member | declarative dataclass |
| `omnigent/_wrapper_labels.py` | `X_NATIVE_WRAPPER_VALUE = "..."` constant | declarative const (⚠ wire protocol) |
| `omnigent/onboarding/harness_install.py` | `HarnessInstallSpec` entry + name→key map rows | declarative dict |
| `omnigent/onboarding/harness_readiness.py` | a per-harness frozenset + aggregation line | declarative set |
| `omnigent/model_override.py` | member of `_SDK_MODEL_OVERRIDE_HARNESSES` | declarative set |
| `omnigent/reasoning_effort.py` | provider→effort mapping row | declarative dict |
| `omnigent/runner/app.py` | `if harness_name == "X":` **spawn_env** branch + **terminal auto-create** block + status-suppression set members | **imperative if-chain** (worst) |
| `omnigent/resume_dispatch.py` | `if key == "X": run_X_native(...)` branch | **imperative if-chain** |
| `omnigent/server/app.py` | `_ensure_default_X_agent()` fn + call in `_ensure_default_agents()` | **imperative fn + call** |
| `omnigent/cli.py` | setup-menu sentinel + row block + dispatch branch + `_manage_X_harness()` fn + `omnigent X` launch | **imperative** |
| `omnigent/server/routes/sessions.py` | `if harness == X` launch-arg / subagent-label branch | **imperative if-chain** |
| `web/src/lib/nativeCodingAgents.ts` | spec-array entry + alias-map row | declarative array (mirror of BE) |
| `web/src/lib/agentGrouping.ts` | `BUILTIN_AGENTS` member + `AGENT_DISPLAY_ORDER` row | declarative (two lists must align) |
| `web/src/shell/SubagentsPanel.tsx` | icon `if`-branch ×2 | imperative switch |
| `web/src/shell/sidebarNav.ts` | `ConversationIconKind` union member | declarative type |
| tests: `test_resume_dispatch.py`, `test_sessions_native_messages.py`, `test_run_harness_without_agent_e2e.py` (exclusion set), web `.test.ts` | per-harness cases / exclusions | mixed |
| `docs/AGENT_YAML_SPEC.md` | harness in the example list | declarative comment |

Two distinct sub-problems fall out:

- **Declarative scatter** (most rows above): the same harness identity is re-keyed in
  ~10 different containers. Conceptually one record split across many files.
- **Imperative scatter** (`runner/app.py`, `resume_dispatch.py`, `server/app.py`,
  `cli.py`, `sessions.py`): structurally-identical `if harness == "X":` branches that
  each call a *different* per-harness function. Git can't keep-both these cleanly — this
  is what makes the merge treadmill brutal.

### 1b. Capability opacity
Whether a harness supports a feature is implicit — encoded in (a) membership of ad-hoc
frozensets (`NATIVE_HARNESSES`, `_SDK_MODEL_OVERRIDE_HARNESSES`, the two status-suppression
sets in `runner/app.py`), and (b) **presence/absence of a module** (e.g.
`codex_native_elicitation.py` exists; `*_native_hook.py` exists for
claude/codex/kimi/goose/hermes; `*_native_permissions.py` exists for
cursor/goose/hermes/kiro/opencode/qwen). There is no single place that answers "what can
this harness do?" — and the scatter actively misleads: an early read of this code assumed
"kiro-native has no elicitation", but the evidence (`kiro_native_permissions.py`: "TUI ACP
recorder -> web elicitation") shows it surfaces approval cards via APPROVAL_MIRROR. A
declared, code-backed capability set removes exactly this guesswork.

---

## 2. What already exists (build on, don't replace)

Three proto-registries are already in the tree:

1. **`_HARNESS_MODULES`** (`runtime/harnesses/__init__.py`): `harness name → module exporting
   create_app()`. This is the spawn/dispatch registry. Still a central dict everyone edits.
2. **`NATIVE_CODING_AGENTS`** (`native_coding_agents.py`): a tuple of frozen
   `NativeCodingAgent` records + four derived `_BY_*` lookup dicts. This is already the
   *right shape* — a per-harness declarative record with lookup indexes. It just (a) only
   covers native TUIs, (b) holds only UI/wire fields, and (c) is hand-maintained.
3. **`Executor` capability methods** (`inner/executor.py:541-587`):
   `supports_streaming()`, `handles_tools_internally()`, `supports_live_message_queue()`,
   `supports_tool_boundary_interrupt()`, `supports_stepwise_internal_turns()`,
   `max_context_tokens()`. These are **already declarative capabilities** — but they live
   in the harness *subprocess* and are only observable *after* spawn, so the framework
   can't use them for routing/UI/install decisions.

**Design consequence:** we don't invent a registry; we (i) generalize `NativeCodingAgent`
into a full `HarnessDescriptor` that covers all integration modes, (ii) make every
scattered container *derive from* the descriptor registry, and (iii) split capabilities
into a **static** layer (framework-side, in the descriptor) and the existing **runtime**
layer (subprocess-side, on `Executor`), with a test asserting they don't contradict.

---

## 3. Target design

### 3.1 One descriptor per harness
```python
# omnigent/harnesses/types.py
@dataclass(frozen=True)
class HarnessCapabilities:
    integration_mode: IntegrationMode      # SDK_IN_PROCESS | NATIVE_TUI | NATIVE_SERVER | ACP_HEADLESS
    elicitation: Elicitation               # NONE | HOOK | JSONRPC | APPROVAL_MIRROR | SSE_PERMISSION
    resume: Resume                         # COLD_ONLY | WARM_REATTACH
    model_override: bool
    model_family: ModelFamily              # CLAUDE | GPT | GEMINI | MULTI
    effort: EffortFamily                   # NONE | ANTHROPIC | OPENAI | GEMINI
    permission_enforcement: frozenset[PermissionMech]  # {LAUNCH_FLAG, RUNTIME_HOOK, APPROVAL_CARD}
    web_bridge: WebBridge                  # NONE | TERMINAL_TAKEOVER | APP_SERVER_SSE | RPC
    subagents: bool
    auth: AuthModel                        # OMNIGENT_CREDENTIAL | OWN_AUTH | SESSION_SCOPED_CONFIG

@dataclass(frozen=True)
class HarnessDescriptor:
    # identity
    name: str                              # canonical, e.g. "kiro-native"
    aliases: tuple[str, ...]               # ("native-kiro", ...)
    display_name: str
    # spawn / dispatch
    harness_module: str                    # dotted path exporting create_app()  (was _HARNESS_MODULES)
    # capabilities (single source of truth — §1b)
    capabilities: HarnessCapabilities
    # UI / wire metadata (superset of today's NativeCodingAgent; None for pure-SDK)
    native: NativeUIMeta | None            # agent_name, wrapper_label, terminal_name,
                                           # subagent_wrapper_label, icon_kind, sort_rank
    # install / onboarding (was HarnessInstallSpec)
    install: HarnessInstallSpec | None
    # imperative hooks — lazily-imported callables, NOT inline if-branches (§3.3)
    hooks: HarnessHooks
```

`HarnessHooks` holds the per-harness *callables* that today live inside the if-chains:
`build_spawn_env`, `ensure_terminal`, `run_native` (resume), `ensure_default_agent`,
`manage_cli`, `terminal_launch_args`. Each is an optional dotted-path / lazy import so the
framework iterates the registry instead of branching on the name. Pure-SDK harnesses leave
most of these `None`.

### 3.2 Discovery — explicit import list as the single contention point (DECIDED)
Each harness becomes a package `omnigent/harnesses/<name>/` exposing
`DESCRIPTOR: HarnessDescriptor`. Rather than runtime auto-discovery, the registry is built
from **one explicit, append-only, sorted import list** — the deliberate trade: accept a
single trivially-mergeable central file instead of zero, in exchange for explicitness and
import-order determinism.
```python
# omnigent/harnesses/registry.py  — the ONLY file edited per new harness
from omnigent.harnesses.claude_native import DESCRIPTOR as claude_native
from omnigent.harnesses.codex_native import DESCRIPTOR as codex_native
# ... one sorted line per harness ...
_ALL = (claude_native, codex_native, ...)          # sorted; append-only
REGISTRY = {d.name: d for d in _ALL}
```
Why this is acceptable as the lone contention point: each PR adds exactly **one import line
and one tuple entry**, both kept alphabetically sorted, so git conflicts here are
mechanical one-liners (unlike the §1a if-chains git cannot untangle). To stop silent
omissions, a test asserts the import list exactly matches the `omnigent/harnesses/*`
directory glob — a forgotten registration fails CI loudly. This keeps the "one dir per
harness" story for everything *except* a single sorted manifest.

**Adding a harness = create `omnigent/harnesses/<name>/` + add one sorted import line.** All
other §1a central edits disappear.

### 3.3 Imperative if-chains → registry iteration
The high-value change. Today:
```python
# runner/app.py (×11, structurally identical, git-untangleable)
if harness_name == "claude-native" and spawn_env is None:
    from omnigent.claude_native_bridge import build_claude_native_spawn_env
    spawn_env = build_claude_native_spawn_env(...)
if harness_name == "codex-native" and spawn_env is None:
    ...
```
After:
```python
d = registry.get(harness_name)
if spawn_env is None and d and d.hooks.build_spawn_env:
    spawn_env = d.hooks.build_spawn_env(...)   # lazy import of the harness's own fn
```
Same collapse for `resume_dispatch._dispatch_wrapper`, `server/app._ensure_default_agents`,
`cli` setup menu/dispatch, and `sessions.py` launch-arg branches. The per-harness function
bodies don't change — they move into the harness package and are *referenced* by the
descriptor instead of *named* in a central conditional.

The spawn_env builders already share a uniform signature
(`build_<x>_native_spawn_env(...)`), which is what makes this collapse safe.

### 3.4 Capability matrix as a first-class artifact
- `omni harness matrix` renders harness × capability from the registry.
- A committed `docs/HARNESS_CAPABILITIES.md` table is generated from the registry; a CI
  test asserts it's regenerated (no drift).
- Static (descriptor) vs runtime (`Executor`) capabilities: a test asserts overlapping
  axes agree (e.g. a descriptor claiming `WARM_REATTACH` must back a harness whose executor
  implements the reattach path).

Then a harness literally declares its elicitation mechanism (e.g. `pi-native` →
`elicitation=NONE`, `kiro-native` → `APPROVAL_MIRROR`, `codex-native` → `JSONRPC`), and code
that needs elicitation queries `d.capabilities.elicitation` instead of probing for a
module's existence.

---

## 4. Capability inventory (the real axes, from code)

The exhaustive, evidence-backed harness × capability values now live **in code** as the
single source of truth: `omnigent/harnesses/capabilities.py` (declarations) rendered by
`omnigent/harnesses/matrix.py` (run `python -m omnigent.harnesses.matrix`). This supersedes
the hand-maintained draft table that used to live here — every value there had been verified
against the implementing module and several early assumptions were corrected (notably
`kiro-native`, which is `APPROVAL_MIRROR`, not "no elicitation").

The modeled axes are: `integration_mode` (SDK_IN_PROCESS / CLI_SUBPROCESS / ACP_SUBPROCESS /
NATIVE_TUI / NATIVE_SERVER), `elicitation` (NONE / HOOK / JSONRPC / APPROVAL_MIRROR /
SSE_PERMISSION), `resume` (WARM_REATTACH / COLD_ONLY), `effort` (NONE / ANTHROPIC / OPENAI /
GEMINI / COPILOT), `model_family` (CLAUDE / GPT / GEMINI / MULTI), `auth`
(OMNIGENT_CREDENTIAL / OWN_AUTH / SESSION_SCOPED_CONFIG), and `subagents`. Two axes are
*derivable* and asserted against their source in tests (`model_family` from
`model_override`'s family sets; `subagents` from `NativeCodingAgent.subagent_wrapper_label`);
the rest are declared with an inline evidence comment.

Additional axes the code also branches on but that are *derivable* from the above (so they
stay out of the descriptor to keep it small): status-suppression policy (full vs idle-only),
terminal auto-create, history-synthesis at cold resume, app-server lifecycle ownership,
session-scoped credential synthesis.

---

## 5. Migration plan (incremental, non-breaking)

Hard constraint: **wrapper-label string values are wire protocol** (DB-persisted, used for
resume). Migration moves *where data lives and how it's read*, never the values.

The safety mechanism for every phase: a **golden equivalence test** asserting
`registry-derived value == today's scattered constant` for every harness, so each repoint
is provably behavior-preserving.

**Phase 0 — Registry skeleton, zero behavior change.**
Add `HarnessDescriptor`/`HarnessCapabilities`/`registry.py`. Initially each descriptor is
*derived from* existing constants (adapter shims), and descriptors live in one transitional
module. Land the golden test. Outcome: a single read-path exists; nothing consumes it yet.

**Phase 1 — Declarative consumers read the registry.**
Repoint `NATIVE_HARNESSES`, `HARNESS_ALIASES`, `NATIVE_CODING_AGENTS`, `_HARNESS_MODULES`,
`_SDK_MODEL_OVERRIDE_HARNESSES`, install specs, readiness sets, effort maps to be *computed
from* the registry. Keep the old names as thin re-exports so unrelated imports don't break.
Outcome: the scattered containers become generated, not authored.

**Phase 2 — Imperative if-chains → registry iteration (§3.3).** Highest value; riskiest.
One call site at a time, behind the golden test: `runner/app.py` spawn_env, then terminal
auto-create, then `resume_dispatch.py`, `server/app.py`, `cli.py`, `sessions.py`. Function
bodies unchanged — only the dispatch mechanism changes. This is what kills the merge
treadmill.

**Phase 3 — Relocate per-harness code into `omnigent/harnesses/<name>/` (full move).**
Mechanical file moves (executor, native wrapper, bridge/forwarder/hook, descriptor) into
self-contained packages, harness by harness. Flip `registry.py` from the transitional
module to the explicit import manifest (§3.2) + dir-glob test. After this, the central
files from §1a no longer carry per-harness entries.

**Phase 4 — Frontend codegen + capability surfacing (document-only).**
Export the registry as JSON and **generate** `nativeCodingAgents.ts` / `agentGrouping.ts`
from it (BE is source of truth); commit the generated files + a CI check that they're
regenerated. Ship `omni harness matrix`, the generated `HARNESS_CAPABILITIES.md`, and the
static-vs-runtime capability consistency test. Behavioral enforcement of capabilities
(graceful "not supported") is explicitly deferred past v1 (decision §6.3).

Each phase is independently shippable and leaves the tree green.

---

## 6. Decisions (RESOLVED)

1. **Discovery mechanism → explicit import list (§3.2).** One sorted, append-only manifest
   is the single accepted contention point; a dir-glob test prevents silent omissions.
2. **Frontend parity → generate `nativeCodingAgents.ts` / `agentGrouping.ts` from a
   BE-exported JSON.** Backend registry is the source of truth; FE is a generated artifact
   (strongest anti-drift). Adds a codegen step to the build (Phase 4).
3. **Capabilities in v1 → document/route only.** Descriptor + matrix become the source of
   truth for routing, UI, install, and docs. Behavioral gating (graceful "not supported" on
   a missing capability) is deferred to a follow-up pass, keeping v1 surface area smaller.
4. **Phase 3 file moves → full relocation into `omnigent/harnesses/<name>/`.** Move every
   per-harness module (executor, native wrapper, bridge/forwarder/hook, descriptor) into the
   package, delivering the complete "one dir per harness" story. Large but mechanical diff,
   done harness-by-harness behind the golden test.

---

## 7. Out of scope / risks
- Renaming any wrapper-label/agent-name string value (wire protocol — frozen).
- The `cli.py` `_manage_*_harness` functions carry genuinely bespoke interactive auth flows;
  they relocate as-is behind a `hooks.manage_cli` reference but are not unified.
- antigravity-native's RPC stack and opencode-native's app-server are the least uniform; do
  them last in Phase 2/3.
