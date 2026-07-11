# Director Session: `session_control` Grant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an Omnigent agent session ("director session") drive the lifecycle of OTHER sessions it can already see: resolve a pending approval/input elicitation in another session, interrupt another session's running turn, or stop another session's live process. Formalize this as a new spec-level grant, `session_control: true`, mirroring the existing `spawn:` / `agent_session_sharing:` grants. A session can never resolve, interrupt, or stop **its own** session — every one of the three new tools requires an explicit, non-self target.

**Architecture:** The REST endpoints already exist and are already reachable by the runner's authenticated `server_client` for the user's own sessions (`POST /v1/sessions/{id}/elicitations/{eid}/resolve`, `POST /v1/sessions/{id}/events` with `type: interrupt` / `type: stop_session`). What's missing is the spec-level opt-in and the tool surface. Three new schema-only `Tool` subclasses in `omnigent/tools/builtins/spawn.py` are gated behind the new `session_control` grant in `ToolManager._register_sub_agent_tools`, and dispatched by three new runner-side REST-proxy helpers in `omnigent/runner/tool_dispatch.py` (mirroring the existing `_session_get_info_via_rest` / `_session_share_via_rest` pattern). The runner enforces the spec grant and the not-own-session guard, since the server cannot see spec-level grants (same pattern as `agent_session_sharing`). No server-side changes are required — enforcement is runner-side only, per the `agent_session_sharing` precedent. Discovery needs no new tool: `sys_session_get_info` already projects the target session's `pending_elicitations` (ids + message + phase + policy_name).

**Tech Stack:** Python 3, pytest, existing `omnigent.spec` / `omnigent.inner.datamodel` / `omnigent.tools.manager` / `omnigent.runner.tool_dispatch` plumbing.

**Related docs:** Plan originated from an interactive planning session; see `/home/jason/.claude/plans/precious-weaving-swan.md` for the original plan-mode writeup (superseded by this doc as the durable record).

## Global Constraints

- Grant key is exactly `session_control` (top-level YAML boolean, like `spawn:`); defaults to `False`.
- All three new tools (`sys_session_resolve_elicitation`, `sys_session_interrupt`, `sys_session_stop`) REQUIRE an explicit `session_id` argument — no default-to-caller fallback (deliberate divergence from `sys_session_get_info` / `sys_session_share`).
- All three tools MUST reject `session_id == conversation_id` (the caller's own session) with a typed error, before any HTTP call.
- `sys_session_resolve_elicitation` MUST also reject resolving an elicitation that is merely *mirrored* into another session but actually owned (via `params.target_session_id`) by the caller's own session — this closes a self-approval bypass through ancestor mirroring.
- The runner is the sole enforcement point for the `session_control` spec grant (the server has no visibility into spec-level grants) — mirror `_session_share_via_rest`'s gate.
- No server-side (`omnigent/server/routes/sessions.py`) changes in this plan — decided to skip the optional self-approval backstop query param; runner-side guards are the only enforcement.
- Sending messages to native terminal sessions is explicitly OUT of scope.
- Native harness static allow-lists (`cursor_native_bridge.py`, `antigravity_native_bridge.py`) must include the three new tool names, kept alphabetically sorted (existing tests assert sortedness for antigravity).
- Follow repo comment rules (CLAUDE.md): short comments describing the scenario, never the PR/change history. Methods stay under the codebase's informal ~40-line guideline.
- Run all commands from `/home/jason/workspace/omnigent/.claude/worktrees/director-session` (branch `worktree-director-session`).
- Commit each task separately; `git commit` runs the pre-commit hook — fix anything it reports rather than skipping it.

---

### Task 1: Spec plumbing — `session_control` grant field

**Status:** Done (already applied in this worktree).

**Files:**
- `omnigent/spec/types.py` — `AgentSpec.session_control: bool = False` (+ docstring), after `agent_session_sharing`.
- `omnigent/spec/parser.py` — parse `raw.get("session_control", False)`, pass into `AgentSpec(...)`.
- `omnigent/inner/datamodel.py` — `AgentDef.session_control: bool = False`.
- `omnigent/inner/loader.py` — `agent.session_control = data.get("session_control", False)`.
- `omnigent/spec/omnigent.py` — translator passes `session_control=agent_def.session_control` into the `AgentSpec(...)` construction.

**Steps:**
- [x] Add `AgentSpec.session_control` field + docstring in `omnigent/spec/types.py`.
- [x] Parse `session_control` in `omnigent/spec/parser.py` and thread into the returned `AgentSpec`.
- [x] Add `AgentDef.session_control` field in `omnigent/inner/datamodel.py`.
- [x] Read `session_control` in `omnigent/inner/loader.py`.
- [x] Map `agent_def.session_control` in the `omnigent/spec/omnigent.py` translator.

**Test task:**
- [x] `tests/spec/test_parser.py`: `test_parse_session_control_defaults_to_false_when_omitted`, `test_parse_session_control_true_sets_flag` (mirror `test_parse_spawn_*` at ~line 2462/2479).

---

### Task 2: Tool classes + `ToolManager` registration

**Files:**
- Modify: `omnigent/tools/builtins/spawn.py` — add `SysSessionResolveElicitationTool`, `SysSessionInterruptTool`, `SysSessionStopTool` after `SysSessionShareTool` (~line 647).
- Modify: `omnigent/tools/builtins/__init__.py` — export the three new classes.
- Modify: `omnigent/tools/manager.py` — import the three classes (~line 32-37 block) and register them in `_register_sub_agent_tools` (~line 441, new arm after the `agent_session_sharing` arm): `if self._spec.session_control: ...`.
- Test: `tests/tools/test_manager.py` — new cases after the share-tool tests (~line 517).

**Interfaces:**
- Each tool class follows the `SysSessionShareTool` pattern: `classmethod name()`, `classmethod description()`, `get_schema()` returning an OpenAI function-tool schema dict. These are schema-only — real dispatch happens in the runner (Task 3).
- `sys_session_resolve_elicitation`: params `session_id` (string, required), `elicitation_id` (string, required), `action` (string enum `accept`/`decline`/`cancel`, required), `content` (object, optional — form data for `accept`). `additionalProperties: false`.
- `sys_session_interrupt`: params `session_id` (string, required). `additionalProperties: false`.
- `sys_session_stop`: params `session_id` (string, required). `additionalProperties: false`.
- Every description must state: discover pending items via `sys_session_get_info`; the target must not be the caller's own session; `sys_session_stop` requires owner-level access on the target and is non-sticky (a later message relaunches it).

**Steps:**
- [x] Implement `SysSessionResolveElicitationTool`, `SysSessionInterruptTool`, `SysSessionStopTool` in `omnigent/tools/builtins/spawn.py`.
- [x] Export the three classes from `omnigent/tools/builtins/__init__.py`.
- [x] Import the three classes in `omnigent/tools/manager.py` and add the `if self._spec.session_control:` registration arm in `_register_sub_agent_tools`.
- [x] `tests/tools/test_manager.py`: `test_session_control_registers_control_tools` (grant on, no spawn/sub-agents, asserts all three registered), `test_session_control_off_leaves_tools_unregistered` (default spec).

---

### Task 3: Runner dispatch — REST proxy helpers + guards

**Files:**
- Modify: `omnigent/runner/tool_dispatch.py`:
  - New frozenset `_SESSION_CONTROL_TOOLS` after `_SESSION_QUERY_TOOLS` (~line 226); add `| _SESSION_CONTROL_TOOLS` to `_NATIVE_RELAY_BUILTIN_TOOLS` (~line 322-334).
  - New `elif tool_name in _SESSION_CONTROL_TOOLS:` arm in the `execute_tool` dispatch ladder (~line 4106) calling `_execute_session_control_tool(...)`.
  - New `_execute_session_control_tool` near `_execute_session_query_tool` (~line 2794): shared preamble (null checks, JSON parse, spec gate, explicit-`session_id` + not-own-session guards), then dispatch to per-tool helpers.
  - New `_session_resolve_elicitation_via_rest`, `_session_interrupt_via_rest`, `_session_stop_via_rest` helpers (+ a `_find_pending_elicitation_owner` sub-helper for the mirror-owner check).
- Test: `tests/runner/test_runner_dispatch.py` — new cases mirroring the `sys_session_share` block (~line 6274-6535).

**Interfaces:**
- Spec gate: `getattr(agent_spec, "session_control", False) is not True` → `{"error": "session control is not enabled for this agent (set session_control: true in the spec)"}` (mirrors the `agent_session_sharing` gate at `_session_share_via_rest`, ~line 3053-3075).
- Not-own-session guard: `session_id == conversation_id` → `{"error": "cannot_target_own_session", "session_id": session_id}`.
- `_session_resolve_elicitation_via_rest`: validate `action` in `{"accept", "decline", "cancel"}` and `content` is dict-or-absent; `GET /v1/sessions/{target}` to find the matching entry in `pending_elicitations`; missing → `{"error": "elicitation_not_found", "session_id": target, "elicitation_id": ...}`; compute `owner = entry["params"].get("target_session_id") or target`; if `owner == conversation_id` → `cannot_target_own_session`; else `POST /v1/sessions/{owner}/elicitations/{elicitation_id}/resolve` with `{"action": ..., "content": ...}` (omit `content` when absent); success → `{"resolved": true, "session_id": owner, "elicitation_id": ..., "action": ...}`.
- `_session_interrupt_via_rest`: `POST /v1/sessions/{target}/events` body `{"type": "interrupt", "data": {}}`; success → `{"interrupted": true, "session_id": target}`.
- `_session_stop_via_rest`: `POST /v1/sessions/{target}/events` body `{"type": "stop_session", "data": {}}`; success → `{"stopped": true, "session_id": target}`; 503 (runner unreachable, stop did not land) surfaced via `_omnigent_error_message`.
- Error envelope convention shared with `_session_get_info_via_rest` / `_session_share_via_rest`: 404 → `session_not_found`, 401/403 → `access_denied`, other non-2xx → `_omnigent_error_message(resp)` fallback. Timeouts 30.0s.

**Steps:**
- [x] Add `_SESSION_CONTROL_TOOLS` frozenset and fold into `_NATIVE_RELAY_BUILTIN_TOOLS`.
- [x] Add the `execute_tool` dispatch arm.
- [x] Implement `_execute_session_control_tool` shared preamble + guards.
- [x] Implement `_session_resolve_elicitation_via_rest` + `_find_pending_elicitation_owner`.
- [x] Implement `_session_interrupt_via_rest`.
- [x] Implement `_session_stop_via_rest`.
- [x] `tests/runner/test_runner_dispatch.py`:
  - [x] `test_sys_session_resolve_elicitation_posts_verdict`
  - [x] `test_sys_session_resolve_elicitation_rejects_own_session` (zero HTTP requests)
  - [x] `test_sys_session_resolve_elicitation_rejects_mirrored_own_elicitation`
  - [x] `test_sys_session_resolve_elicitation_unknown_id_errors`
  - [x] `test_sys_session_interrupt_posts_event`
  - [x] `test_sys_session_stop_posts_event`
  - [x] `test_sys_session_control_maps_error_statuses` (parametrized tool × 404/401/403)
  - [x] `test_sys_session_stop_surfaces_runner_unreachable` (503)
  - [x] `test_session_control_disabled_without_grant` (parametrized over all three tools, zero requests)
  - [x] `test_session_control_requires_explicit_session_id` (omitted `session_id` errors, no default-to-caller)

---

### Task 4: Native bridge allow-lists

**Files:**
- `omnigent/cursor_native_bridge.py` — `_CURSOR_AUTO_APPROVE_TOOLS` (~line 37).
- `omnigent/antigravity_native_bridge.py` — `_AGY_ENABLED_TOOLS` (~line 297).
- Test: `tests/test_cursor_native_bridge.py`, `tests/test_antigravity_native_bridge.py`.

**Steps:**
- [x] Add `sys_session_interrupt`, `sys_session_resolve_elicitation`, `sys_session_stop` to `_CURSOR_AUTO_APPROVE_TOOLS`, keeping alphabetical order.
- [x] Add the same three names to `_AGY_ENABLED_TOOLS`, keeping alphabetical order.
- [x] Confirm/extend the existing sortedness + config-emission assertions in both bridge test files to cover the new names.
- [x] No change needed for the claude-native relay (dynamic; already covered by `_NATIVE_RELAY_BUILTIN_TOOLS` in Task 3).

---

### Task 5: Docs + example director agent

**Files:**
- `omnigent/spec/AGENTSPEC.md` — new "Capability grants" subsection covering `spawn`, `agent_session_sharing`, and `session_control`.
- `examples/director/config.yaml`, `examples/director/AGENTS.md` (or `instructions.md`) — new example agent.

**Steps:**
- [x] Document `session_control` in `omnigent/spec/AGENTSPEC.md`: what it enables, the never-own-session rule, `sys_session_stop`'s owner-access requirement, and that discovery uses the existing `sys_session_get_info` read tool.
- [x] Create `examples/director/` with `session_control: true` (+ `spawn: true`) and instructions describing the orchestration loop: `sys_session_list` → `sys_session_get_info` (watch `pending_elicitations`) → `sys_session_resolve_elicitation` / `sys_session_interrupt` / `sys_session_stop` / `sys_session_send`. Mirror the layout of `examples/debby` or `examples/sentinel`.

---

### Task 6: Full verification pass

**Steps:**
- [x] `uv run pytest tests/spec/test_parser.py tests/tools/test_manager.py tests/runner/test_runner_dispatch.py tests/test_cursor_native_bridge.py tests/test_antigravity_native_bridge.py`
- [x] `pre-commit run` on changed files (equivalent to the all-files hook set for the touched paths).
- [ ] Manual end-to-end smoke: boot a local server + runner, launch `examples/director` alongside a worker agent that has an ASK policy; from the director session: list sessions, read the worker's `pending_elicitations` via `sys_session_get_info`, resolve it (`accept`), then interrupt and stop the worker; separately confirm `sys_session_resolve_elicitation` targeting the director's own session id returns `cannot_target_own_session`.
