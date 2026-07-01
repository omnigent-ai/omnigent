> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.

# Executor & Harnesses

Scope: the inner `Executor` ABC + its 4 in-scope implementations (claude-sdk, codex-sdk,
claude-native, codex-native), the adapter that drives them, and the SDK-vs-native split.
**Polly = a custom agent that runs *on* one of these harnesses (typically claude-sdk) and
inherits that row** — it is not its own executor. All anchors below were opened and confirmed
against the worktree (`master-arch-docs`, main + telemetry PR #1617).

---

## 1. Role & boundaries

The **Executor** is the leaf of the turn loop: given `(messages, tools, system_prompt, config)`
it yields a stream of `ExecutorEvent`s for ONE turn. It is the seam where Omnigent's abstract
message/tool model meets a concrete LLM/agent backend (`executor.py:518-537`).

It owns:
- Translating a backend's native output into the canonical `ExecutorEvent` hierarchy.
- Declaring *capabilities* (streaming, interrupt, queue, internal-tool-loop, context window) so
  the adapter/runner know how to drive it (`executor.py:541-587`).
- Per-session backend state (live SDK client / app-server thread / tmux bridge handle) and its
  teardown (`close_session`).

It does **NOT** own: durable history (the runner/server do), policy decisions (server, via the
adapter's policy-evaluator bridge), tool dispatch routing (the scaffold's `dispatch_tool`),
SSE/WS framing (the workflow's `_event_to_sse_dict`), or the request lifecycle. The executor is
called *inside* `ExecutorAdapter.run_turn` (`runtime/harnesses/_executor_adapter.py:394`), which
is itself driven by the harness scaffold/runner.

**The defining axis (SDK vs native):**
- **SDK harness** (claude-sdk, codex-sdk): an **in-process agent loop**. Omnigent owns the
  prompt + tool set + turn loop; the executor runs the vendor SDK/CLI as a child, bridges
  Omnigent tools in as MCP tools, and reconstructs events from the SDK message stream. Transcript
  is 100% Omnigent. `handles_tools_internally() == True` (`claude_sdk_executor.py:1611`,
  `codex_executor.py:2237`).
- **Native harness** (claude-native, codex-native): the executor does **not** run the model. A
  separate wrapper has already launched the *vendor* CLI/TUI (Claude Code / Codex app-server) in
  a tmux pane / on a UDS socket, with the vendor owning its own prompt + tool surface. The
  executor's `run_turn` only **injects the latest user message** into that resident process and
  returns; a separate always-on **forwarder** mirrors the vendor transcript back as
  `external_*` events. `supports_streaming() == False` for both
  (`claude_native_executor.py:64`, `codex_native_executor.py:60`) — output never flows through
  the executor.

---

## 2. Key files & entrypoints (verified)

- `omnigent/inner/executor.py` — ABC + events.
  - `ExecutorConfig` (`:70-88`): `model`, `temperature`, `max_tokens`, `extra` (carries
    `reasoning_effort`, `max_tokens`).
  - Event hierarchy: `TextChunk:102`, `ReasoningChunk:114` (event_type ∈
    `reasoning_text`/`reasoning_summary`/`reasoning_started`), `ToolCallRequest:134`,
    `TurnComplete:150` (`response`/`continue_turn`/`usage`/`modified_by_policy`),
    `ToolCallStatus:178`, `ToolCallComplete:186` (emitted by Session, not executor — `:187`),
    `CompactionComplete:214` (`compacted_messages` = `None` when harness can't export, e.g.
    claude-sdk — `:227`), `TurnCancelled:237`, `ExecutorError:245` (`retryable` flag).
  - Base capability defaults (`:541-587`): `supports_streaming=False`, `supports_tool_calling=True`,
    `handles_tools_internally=False`, `max_context_tokens=None`, `interrupt_session→False`,
    `enqueue_session_message→False`, `supports_live_message_queue=False`,
    `supports_tool_boundary_interrupt=False`, `supports_stepwise_internal_turns=False`. **Every
    default is ❌/None except `supports_tool_calling`.** This matters: an executor that does NOT
    override a capability silently inherits the ❌.
  - Helpers: `split_transient_tail:357` (strips trailing `metadata.framework` notices so a
    history cursor doesn't skip them), `classify_tool_result:409` (status inference for
    ToolCallComplete), `iterate_blocking_stream:292` (sync-iterator→async bridge for codex).
- `omnigent/inner/claude_sdk_executor.py` (2682 lines) — `ClaudeSDKExecutor`. run_turn `:1817`;
  capabilities `:1605-1621`; `_get_or_create_client:1337` (mid-session `set_model` at `:1422`);
  `interrupt_session:1477`; `enqueue_session_message:1509`; gateway env `_resolve_gateway_env:719`.
- `omnigent/inner/codex_executor.py` (2381 lines) — `CodexExecutor`. run_turn `:2319`;
  capabilities `:2231-2241`; `interrupt_session:2243`; `enqueue_session_message:2276`;
  `_ensure_app_session:2294` (signature-keyed teardown/rebuild).
- `omnigent/inner/claude_native_executor.py` (258 lines) — `ClaudeNativeExecutor`. run_turn `:99`
  (tmux inject via `inject_user_message`, span `claude_native.inject` `:143`);
  `enqueue_session_message:72`; `_inject_lock:62` (serializes pane writes).
- `omnigent/inner/codex_native_executor.py` (434 lines) — `CodexNativeExecutor`. run_turn `:145`
  (UDS JSON-RPC `turn/start`/`turn/steer`); `interrupt_session:116` (`turn/interrupt` RPC);
  `enqueue_session_message:68` (`turn/steer`); `_model_effort_overrides:266` (`thread/settings/update`).
- `omnigent/runtime/harnesses/_executor_adapter.py` (the turn loop) — `run_turn:239`; builds
  `ExecutorConfig(model=request.model_override, extra={reasoning_effort, max_tokens})` `:273-284`;
  consume loop `:394`; interrupt-between-events `:407`; injection watcher `:363`.
- `omnigent/claude_native_bridge.py` — `inject_user_message:2393`, `inject_interrupt:2530`
  (sends `Escape`), `kill_session:2559`, `read/write_active_session_id:926/975`,
  `read_bridge_id:960`, `BRIDGE_ID_LABEL_KEY:70`.
- `omnigent/claude_native_forwarder.py` — `forward_claude_transcript_to_session:677`;
  `external_session_id` one-time mirror `_maybe_mirror_external_session_id:2350`;
  `_post_external_conversation_item:3352` (span `claude_native.forward` `:3377`);
  `_post_external_subagent_start:1115`, `_post_external_session_usage:3513`,
  `_post_external_model_change:3576`, `_post_external_compaction_status` (via `:2599`).
- `omnigent/reasoning_effort.py` — effort vocabularies (`:9-22`), `validate_effort:40`.
- `omnigent/cli.py` — `_dispatch_native_terminal_harness:5740` (called `:6049`).
- `omnigent/runtime/workflow.py` — default model resolution chain `:727-751`.

---

## 3. Internal model (per executor)

**claude-sdk** (`_ClaudeClientState`, `claude_sdk_executor.py:563`): one persistent
`ClaudeSDKClient` per Omnigent session, keyed by `session_key`, cached in `self._clients`. Holds
`client`, current `model`, owning `loop`+`task` (so teardown can detect cross-loop close). Crashed
sessions blacklisted in `_crashed_sessions:1221`. Omnigent tools wired as in-process MCP tools
(`_build_mcp_tools:603`); bare names exposed as `mcp__omnigent__<name>` with a system-prompt bridge
note (`_augment_system_prompt_for_omnigent_mcp_tools:668`). Gateway path = base-URL +
`apiKeyHelper` refresh command + TTL (no static token snapshot — `_resolve_gateway_env:719`).

**codex-sdk** (`_CodexSessionState`): one Codex app-server `_CodexAppServerSession` per session,
keyed by a **signature** `(model, system_prompt, cwd, tool_signature)` (`:2346`). If the signature
changes between turns (e.g. `/model`), `_ensure_app_session:2294` **closes and rebuilds** the
app-server thread (`:2303-2304`) — this is why a mid-session model change "resets" the codex thread.

**claude-native** (`ClaudeNativeExecutor`): stateless except `_bridge_dir`,
`_request_session_id`, `_inject_lock`. No model, no SDK client. The vendor `claude` binary lives in
a tmux pane the wrapper launched; the executor only pastes keystrokes.

**codex-native** (`CodexNativeExecutor`): stateless except bridge dir + lock. Talks to the resident
Codex **app-server** over a UDS socket (`client_for_transport`) using JSON-RPC; reads/writes
`active_turn_id` in bridge state to decide `turn/start` vs `turn/steer`.

---

## 4. Inter-component channels (with trace evidence)

The executor itself has no network edges — it is in-process inside `omni-harness` (SDK) or just
pastes into tmux / hits a local UDS (native). The *observable* edges are the runner↔server↔harness
flows the executor's behavior produces. **The SDK vs native trace contrast is the single clearest
signal of the architecture.**

### SDK turn — conv_32db (claude-sdk, "create probe file, read, echo DONE")
Runner *drives* the harness; harness runs the whole loop in-process and calls *back* for policy.
```
omni-server POST /events ──(WS tunnel)──► omni-runner POST /v1/sessions/{conv}/events  [trace 11]
  omni-runner ─► omni-harness POST /v1/sessions/{conv}/events   (7754 ms — the WHOLE turn)
      omni-harness span agent:trace-probe  (7744 ms — in-process SDK agent loop)
      omni-runner ─► omni-server POST /policies/evaluate  phase=LLM_REQUEST   (model=null, 15 tools)
      omni-runner ─► omni-server POST /policies/evaluate  phase=LLM_RESPONSE  (text_preview…)
      [harness returns the final assistant text; runner persists it]
```
Edge counts (`summary_conv_32db…`): `omni-runner → omni-harness POST /events ×3` (the driving
events), policy phases are **LLM_REQUEST / LLM_RESPONSE** (the SDK executor's
`_policy_evaluator` bridge fires around the in-process LLM call). NO `external_*`, NO `/labels`.

### Native turn — conv_94e6 (claude-native, same prompt)
Runner injects ONE message; the *forwarder* posts the transcript back as a cascade of events.
```
omni-runner POST /v1/sessions/{conv}/events  [trace 11]
  omni-runner ─► omni-harness POST /events  (2755 ms)
      omni-harness span agent:probe-claude-native (2739 ms)
      omni-harness span claude_native.inject   (2738 ms — the tmux send-keys, executor.run_turn)
  ── then the always-on forwarder mirrors the vendor transcript ──  [trace 4]
  omni-runner span claude_native.forward ×4  ─► omni-server POST /events  (external_conversation_item)
  omni-runner ─► omni-server GET /v1/sessions/{conv}/labels ×7   (reading bridge_id / active session labels)
  policy phases = REQUEST / TOOL_CALL / TOOL_RESULT  (the vendor's Bash call, evaluated via hook)
```
Edge counts (`summary_conv_94e6…`): `omni-runner → omni-server POST /events ×14` (forwarder
posting `external_*`), `GET /labels ×7`, but `omni-runner → omni-harness POST /events ×1` (one
inject). **This is the exact inverse of the SDK conv** — proving: SDK = runner→harness drives the
turn; native = vendor CLI runs in tmux, forwarder mirrors transcript, harness is barely touched.

**Why** (from code): claude-native `run_turn` (`claude_native_executor.py:99-153`) does one
`inject_user_message` (tmux send-keys) and yields `TurnComplete(response=None)` — there is no
streaming, so the harness produces almost nothing. The real model output is emitted by
`forward_claude_transcript_to_session` (`claude_native_forwarder.py:677`) reading the vendor's
transcript/JSONL and POSTing `external_conversation_item` (span `claude_native.forward`,
`:3377`). The `/labels` reads resolve the `bridge_id` (`BRIDGE_ID_LABEL_KEY`,
`claude_native_bridge.py:70`) so `--resume`/fork sessions land in the right tmux pane.

| edge | transport | SDK conv (32db) | native conv (94e6) |
|---|---|---|---|
| runner → harness `POST /events` | WS-tunnel→local HTTP | **×3 (drives turn)** | ×1 (one inject) |
| runner → server `POST /events` (`external_*`) | WS-tunnel | 0 | **×14 (forwarder)** |
| runner → server `GET /labels` | WS-tunnel | 0 | **×7 (bridge_id)** |
| runner → server `/policies/evaluate` | WS-tunnel | LLM_REQUEST/LLM_RESPONSE | REQUEST/TOOL_CALL/TOOL_RESULT |

---

## 5. CUJ behaviors (per harness, with ⚠️ failure branches)

**Turn loop (all):** `ExecutorAdapter.run_turn:239` stamps every message with `session_key`
(`:271-272` — critical: without it the SDK keys its client under `"default"` and mid-turn
`enqueue` silently fails), builds `ExecutorConfig` `:284`, then `async for event in
executor.run_turn(...)` `:394`, translating each event to SSE via `_translate_event:437`.

**Interrupt (the corrected cell):** the adapter calls `executor.interrupt_session(session_key)`
between events when `ctx.cancelled` is set (`_executor_adapter.py:407`) AND from
`interrupt()` (`:521`).
- **claude-sdk** `interrupt_session:1477` → `client.interrupt()` (0.5 s timeout) **then always
  `close_session`** — the live client is dropped so the next turn rebuilds full history (incl. the
  runner's `[System: interrupted]` marker), because resumed turns only send the latest user
  message (`:1491-1497`). ✅ real.
- **codex-sdk** `interrupt_session:2243` → `app_session.interrupt_turn()` then `close_session`
  (resets thread_id, same rationale `:2260-2264`). ✅ real.
- **codex-native** `interrupt_session:116` → JSON-RPC `turn/interrupt` over the UDS socket. ✅ real.
- **claude-native** does **NOT override `interrupt_session`** → inherits base `False`
  (`executor.py:569`), so the adapter's call is a no-op. The Stop button works through a
  **separate bridge path**: server route → `runner/app.py:10518` →
  `claude_native_bridge.inject_interrupt:2530` which sends a single **`Escape`** keystroke to the
  tmux pane (Claude Code's TUI cancels on Escape). ⚠️ this is the cell the prior pass got wrong —
  "Stop works" is TRUE but "executor.interrupt_session exists" is FALSE; the two are independent.

**Mid-turn queue / steer (all four ✅, different mechanism):**
- claude-sdk `enqueue_session_message:1509` → `client.query(prompt)` into the live session.
- codex-sdk `:2276` → `app_session.enqueue_message`.
- claude-native `:72` → `inject_user_message` (tmux send-keys), serialized under `_inject_lock`.
- codex-native `:68` → `turn/steer` RPC (reads `active_turn_id`, serialized under `_inject_lock`).
  ⚠️ both native paths share the lock with `run_turn` because the steer/start decision is a
  read-decide-RPC-write that races with the initiating inject (see
  `designs/NATIVE_INJECTION_SERIALIZATION.md`); without it keystrokes interleave ("1"+"2"→"12").

**Model & effort at session start:** resolved in `workflow.py` *before* spawning the harness:
spec `executor.model` → provider `models.default` (`family.default_model`) → bundled catalog
family default (`_catalog_default_model`) → **fail loud** (`:729-750`). Written into the
`HARNESS_*_MODEL` env the executor reads as its `_model_override`. Start-time effort comes from the
NewChatDialog → request.reasoning.effort → `config.extra["reasoning_effort"]`.

**Model & effort mid-session (per harness — mechanism differs):**
- claude-sdk: `cfg.model or self._model_override` (`:1910`); applied to the **live** client via
  `set_model` when `state.model != model` (`:1422-1424`) — no teardown. Effort →
  `options_kwargs["effort"]` + adaptive thinking (`:2011-2033`). ✅ applies without restart.
- codex-sdk: model is part of the app-session **signature** (`:2346`); a change rebuilds the
  app-server thread (`_ensure_app_session:2303`). ⚠️ effective but the codex thread state resets.
- codex-native: `/model` pick → `config.model` + `config.extra["reasoning_effort"]` →
  `_model_effort_overrides:266` → a `thread/settings/update` RPC *before* the bare `turn/start`
  (`:237-251`) — `turn/start` itself takes no model/effort. ✅ persists for this+later turns.
- claude-native: the executor ignores `config` entirely (`run_turn` `del tools, system_prompt,
  config` `:123`); the vendor TUI owns its model. A web `/effort` is mirrored in-pane and the
  statusLine change is reflected back by the forwarder. ⚠️ next-turn / vendor-controlled only.

**Default provider/credential resolution:** spec `executor.auth` block → env → CLI login → ambient
detection. claude-sdk gateway path derives base-URL + `apiKeyHelper` refresh *command* (not a
token) from `~/.databrickscfg` (`_resolve_gateway_env:719`, `_databricks_claude_auth_command:819`)
so the CLI re-mints tokens on long sessions (avoids the 1 h-snapshot fail-closed bug). [→ Auth section]

**Harness switching (`POST /sessions/{id}/switch-agent`, `sessions.py:15416`):** loads the target
bundle before mutating (`:15519`); model_override/reasoning_effort carry over **only within the
same provider family** (`_same_provider_family:15537`); a **native target clears
`external_session_id`** and drops the fork-source directive so the runner rebuilds the native
transcript from this session's own Omnigent items (`:15532-15536`). cursor-native can't replay fork
history (no resumable session file) so it isn't promised one (`:15544`). [Polly switches like any
agent — it's just a bundle.]

**Native bridging (one-time external_session_id + bridge labels + forwarder):**
- `external_session_id` is set **exactly once** via `PATCH /sessions/{id}` (`sessions.py:15144`);
  the store raises `ValueError` on overwrite → 400 INVALID_INPUT (`:15158-15166`). The forwarder
  mirrors it best-effort once per (re)start: `_maybe_mirror_external_session_id:2350` guarded by
  `external_session_id_mirrored` flag (`forwarder:752`), reset to False on `/clear`
  (`:810,846`).
- `bridge_id` is stored as a session **label** (`BRIDGE_ID_LABEL_KEY`,
  `claude_native_bridge.py:70`); fork/clear/resume keep the SAME bridge_id (`:628`) so they share
  one tmux pane — this is why the native conv reads `/labels ×7`.
- The forwarder emits `external_{conversation_item,subagent_start,session_usage,model_change,
  compaction_status}` — the native analog of the SDK executor's in-process event stream.

**Subagents:** gated by the **tool surface**, not an executor flag. SDK harnesses bridge
`sys_session_send`/`sys_session_create` as MCP tools (the model calls them → child session). The
subagent conv_fc47 (claude-sdk) is in the corpus. claude-native surfaces subagents via the
vendor's Task tool → forwarder `_post_external_subagent_start:1115`. codex (both) = implicit via
`CODEX_HOME`/subprocess isolation. [Depth limits / dispatch → Subagents section.]

---

## 6. Answers to doc questions (terse, code-anchored)

- **Executor's role in the turn loop:** the per-turn event generator at the bottom of
  `ExecutorAdapter.run_turn`; everything above it (history, policy, SSE, dispatch) is the
  adapter/scaffold/runner. `executor.py:524`, driven at `_executor_adapter.py:394`.
- **SDK vs native taxonomy & who owns what:** SDK = in-process loop, Omnigent owns
  prompt+tools+transcript, `handles_tools_internally=True`. Native = resident vendor CLI in
  tmux/UDS, vendor owns prompt+tools, executor only injects, forwarder mirrors transcript,
  `supports_streaming=False`.
- **Per-harness capability matrix:** see §7 below (cell-by-cell verified).
- **Model/effort resolution start+mid:** start in `workflow.py:727-750`; mid via per-harness
  mechanism above (sdk live `set_model`; codex-sdk thread rebuild; codex-native
  `thread/settings/update`; claude-native vendor-only).
- **Default model/provider chain:** spec model → provider default → catalog default → fail loud
  (`workflow.py:729-750`); executor adds a per-turn `/model`-override layer on top.
- **Harness switching:** `switch-agent` clears `external_session_id` for native targets, carries
  model settings only within a provider family (`sessions.py:15416,15532`).
- **Reasoning-effort vocab (source of truth `reasoning_effort.py`):**
  `CLAUDE/ANTHROPIC = {low,medium,high,xhigh,max}` (`:13`),
  `CODEX/OPENAI = {none,minimal,low,medium,high,xhigh}` (`:12`). `EFFORT_CLEAR_VALUES =
  {default,off,reset}` (`:10`).

---

## 7. Corrected per-harness capability matrix (cell-by-cell vs code)

Two levels reported because the prior pass conflated them:
**(A) executor-method level** — does the executor *override* the capability (else inherits base ❌)?
**(B) product level** — does the user-facing behavior work (possibly via a non-executor path)?

| Harness | interrupt — exec method | interrupt — product (Stop) | queue (`supports_live_message_queue`) | tool-boundary interrupt | subagents | reasoning-effort | elicitation | mid-session model |
|---|---|---|---|---|---|---|---|---|
| **claude-sdk** | ✅ `interrupt()`+close (`:1477`) | ✅ | ✅ (`:1614`) | ✅ (`:1617`, the **only** harness) | ✅ (`sys_session_*` MCP) | ✅ {low,med,high,xhigh,max} (`:2011`) | ✅ (`_can_use_tool` → elicitation, `:1633`) | ✅ live `set_model` (`:1422`) |
| **codex-sdk** | ✅ `interrupt_turn()`+close (`:2243`) | ✅ | ✅ (`:2240`) | ❌ **not overridden → base False** | ⚠️† subprocess `CODEX_HOME` | ✅ {none,minimal,low,med,high,xhigh} (`:2353`) | ⚠️‡ executor base ❌; forwarder may handle | ⚠️ rebuilds thread (`:2303`) |
| **claude-native** | ❌ **not overridden → base False (no-op)** | ✅ via bridge `inject_interrupt` Escape (`bridge:2530`, called `runner/app.py:10518`) | ✅ tmux inject (`:72`) | ❌ base False | ✅ vendor Task → `external_subagent_start` | ⚠️ vendor-only (`/effort` mirrored, next turn) | ✅ via hook/policy + vendor UI | ⚠️ vendor-only, next turn (config ignored, `:123`) |
| **codex-native** | ✅ `turn/interrupt` RPC (`:116`) | ✅ | ✅ `turn/steer` (`:68`) | ❌ base False | ⚠️† subprocess isolation | ✅ {…openai} via `thread/settings/update` (`:266`) | ✅ forwarder hook | ✅ `thread/settings/update` (`:237`) |

Legend: ✅ confirmed · ⚠️ partial/caveated · ❌ confirmed absent.
† codex subagents = implicit subprocess `CODEX_HOME` isolation, not a declared capability.
‡ codex-sdk elicitation: executor returns base ❌; the forwarder *may* handle it but unverified at
the executor boundary (codex-*native* elicitation IS ✅ via the forwarder hook).
**Polly** has no row — running on claude-sdk it reads exactly as the claude-sdk row.

Key cross-cell facts the §4 matrix flattens: only **claude-sdk** declares
`supports_tool_boundary_interrupt` (`:1617`); codex-sdk does not (so queued input can't be applied
at a tool boundary — base False). `handles_tools_internally=True` only for the two SDK rows; both
native rows leave it base False (the vendor runs its own tools, mirrored). `max_context_tokens`:
claude-sdk returns `None` "SDK manages its own context" (`:1620`).

---

## 8. Reliability gaps / sharp edges (confirmed in code)

1. **Base-default trap:** any executor that forgets to override a capability silently inherits ❌
   (`executor.py:541-587`). This is exactly why claude-native's Stop *looked* broken to a prior
   reader — `interrupt_session` falls through to the base no-op; the working path is the bridge
   Escape, an entirely separate wire. Reviewing the matrix from executor methods alone is wrong.
2. **session_key stamping is load-bearing:** if the adapter didn't stamp `message["session_id"]`
   (`_executor_adapter.py:271`), the SDK keys its client under `"default"` and every mid-turn
   `enqueue_session_message` returns False — the user-reported "steering ignored, Claude keeps
   going" symptom (documented inline `:267-270`).
3. **Interrupt drops the live session (both SDK):** claude-sdk and codex-sdk *always*
   `close_session` after an interrupt (`:1499`, `:2266`) because a resumed transcript would only
   resend the latest user message and bypass the `[System: interrupted]` marker — so an interrupt
   pays a full cold-start + history-replay on the next turn.
4. **claude-native interrupt is best-effort Escape with a 1 s tmux timeout** (`runner/app.py:10518`);
   if `tmux.json` isn't advertised yet it 503s and the Stop is silently a no-op. There's also no
   `[System: interrupted]` marker appended for native (`runner/app.py:10527` comment).
5. **codex-sdk mid-session model = thread teardown** (`:2303`): a `/model` mid-conversation throws
   away the running codex app-server thread (signature change), unlike claude-sdk's in-place
   `set_model`. Cost + potential loss of in-thread state.
6. **codex-native double-start race** is only prevented by `_inject_lock`
   (`codex_native_executor.py:58`); the read-decide(start vs steer)-RPC-write is not atomic
   otherwise → two injects could both see "no active turn" and double-start.
7. **`external_session_id` overwrite = hard 400** (`sessions.py:15158`): if a wrapper bridge ever
   re-PATCHes a different vendor session id (e.g. after an unexpected vendor relaunch with a new
   id), the write fails closed rather than re-binding. The forwarder's mirror flag resets on
   `/clear` (`forwarder:810`) but the server side does not, so a post-`/clear` id must match the
   first or be cleared via switch-agent/fork.
8. **CompactionComplete.compacted_messages = None on claude-sdk** (`executor.py:227`): the CLI's
   compaction is internal, so a resumed claude-sdk session can't replay pre-compacted history the
   way harnesses that export their compacted state can. [→ Compaction section.]

---

## 9. Corrections to CUJ-ANALYSIS

- **§4 matrix, claude-native interrupt cell** — the matrix is now CORRECT (✅ via bridge
  `inject_interrupt`) but the **anchor is stale**: it cites `claude_native_bridge.py:2484`; the
  actual definition is **`:2530`**, and the caller is `runner/app.py:10518` (server route). The
  note's reasoning (executor method ≠ Stop button) is right and verified — claude-native does NOT
  override `interrupt_session` (inherits base False, `executor.py:569`).
- **§4 matrix — missing the tool-boundary-interrupt divergence.** The matrix has no column for it,
  but it's a real per-harness difference: **only claude-sdk** overrides
  `supports_tool_boundary_interrupt` (`:1617`); codex-sdk, claude-native, codex-native all inherit
  base False. Worth a footnote so "queue" isn't read as "queued input applies mid-tool".
- **§2.B / §4, "codex (SDK) mid-session model: per-turn (resets at session)"** — accurate but
  under-specified. The mechanism is an **app-server thread teardown+rebuild** keyed on the
  `(model, system_prompt, cwd, tools)` signature (`codex_executor.py:2303,2346`), not a soft
  per-turn config. Contrast claude-sdk, which mutates the **live** client via `set_model`
  (`:1422`) with no teardown — the §4 "Notes" lump these together as "SDK set_model/per-turn
  config" but they behave differently (claude-sdk keeps session state, codex-sdk loses it).
- **§2.B claude-native mid-session model "✅ (next turn)"** is optimistic: the executor *ignores*
  `config` outright (`claude_native_executor.py:123` `del tools, system_prompt, config`). The
  vendor TUI owns the model; a web change only takes effect insofar as it's mirrored in-pane via
  `/effort`/statusLine — there is no executor-side application. Reads better as ⚠️ vendor-only.
- **§2.B `_dispatch_native_terminal_harness` anchor `cli.py:5740`** — confirmed correct
  (definition at `:5740`, called `:6049`). No drift. (Recorded as a positive confirmation.)
