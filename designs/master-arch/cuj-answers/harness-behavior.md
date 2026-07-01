# CUJ Answers — Harness / Inner Behavior (claude-sdk · claude-native · codex · codex-native · polly)

> Companion to `designs/CUJ-ANALYSIS.md` §2.B / §4 / §2.D. Every cell is **re-verified
> cell-by-cell against the executors** in the merged `traces` worktree. Anchors are
> `file:line`. **codex / codex-native = no live trace (creds 403)** — verified from code only.
> Legend: ✅ confirmed in code · ⚠️ partial/caveated · ❌ confirmed absent.

---

## 0. Headline §4 corrections (read first)

The §4 matrix is **mostly right**; the corrections below are the load-bearing ones.

1. **claude-native interrupt = ✅ (was ❌ in the first pass).** It is **not** wired at the
   executor (`claude_native_executor.py` has no `interrupt_session` override). The web Stop
   button reaches `runner/app.py:_handle_claude_native_interrupt` → `claude_native_bridge.
   inject_interrupt` (`:2530`) → `tmux send-keys … Escape` (`:2556`). Read the interrupt column
   as "the product Stop interrupts the running turn," not "the executor method exists."
   *(The current §4 already shows ✅ with the `inject_interrupt` note — keep it; this confirms it.)*

2. **codex-native subagents = ✅ (matrix footnote † is stale for codex-native).** The footnote
   says "codex subagents = implicit via subprocess CODEX_HOME isolation, not a declared
   capability." That is true for codex **(SDK)** only. **codex-native** has a real subagent
   *mirror*: `codex_native_forwarder.py:6079` (`_thread_started_is_subagent`) detects
   `source.subAgent.thread_spawn` (`:4310`) and POSTs `external_codex_subagent_start`
   (`:130/:4304`) → child Omnigent session. Promote codex-native subagents to ✅; keep the †
   footnote scoped to codex-SDK.

3. **claude-native subagents = ✅ (genuine, not just "tool surface").** The forwarder watches
   `~/.claude/projects/<encoded>/<session>/subagents/agent-*.meta.json` (`:217`) →
   `_post_external_subagent_start` (`:1115`) → `external_subagent_start` (`:1150`). Claude
   Code's Task tool spawns are mirrored as child sessions. (Matrix already ✅ — confirmed.)

4. **codex (SDK) elicitation = ❌ at the executor (confirmed, not just "unverified").** The
   executor hardcodes `"approvalPolicy": "never"` (`codex_executor.py:1427`); there is no
   `approval`/`elicitation` handling at the executor boundary. The matrix ‡ caveat is correct —
   tighten it from "unverified" to "confirmed ❌ at the executor; codex-*native* ✅ via the hook."

5. **codex (SDK) mid-session model = resets the thread (sharper than "per-turn").** Model is
   part of the app-session **signature** (`codex_executor.py:2346-2350`); changing it **closes
   the app-session and starts a fresh thread** (`:2301/:2304`). Effort likewise resets
   (`self._applied_effort = None` on new thread, `:1450`). Matrix "⚠️ per-turn (resets at
   session)" is right; the mechanism is *thread teardown*, not a soft per-turn toggle.

6. **No new harness gains queue/stepwise/tool-boundary-interrupt.** All four executors leave
   `supports_tool_boundary_interrupt()` and `supports_stepwise_internal_turns()` at the base
   `False` — **except claude-sdk**, which overrides `supports_tool_boundary_interrupt()`→True
   (`:1617`). Worth surfacing: it is the only in-scope harness that can apply queued input at a
   tool boundary.

---

## 1. Corrected per-harness capability matrix (code-verified)

> Verified against each `inner/*_executor.py` capability override (base defaults all ❌ except
> `supports_tool_calling`, `executor.py:541-585`) + native bridge/forwarder/runner routes.
> **interrupt** = the web Stop stops the running turn (SDK via executor; native via bridge/RPC).
> **queue** = `supports_live_message_queue()`. **subagents** = surfaces a child session.
> **reasoning effort** = accepts a `reasoning_effort` param (≠ merely streaming thinking).
> **elicitation** = can surface a policy/permission prompt. **mid-session model** = model change
> without a restart.

### SDK harnesses

| SDK harness | interrupt | queue | subagents | reasoning effort | elicitation | mid-session model |
|---|---|---|---|---|---|---|
| **claude-sdk** | ✅ executor `client.interrupt()` (`:1477`) | ✅ (`:1614`) `client.query` (`:1509`) | ✅ via `mcp__omnigent__sys_session_*` | ✅ {low,med,high,xhigh,max} (`:2011`) | ✅ SDK `can_use_tool`/elicitation bridge (`_executor_adapter:307`) | ✅ `set_model()` next turn (`:1422/:1910`) |
| **codex** | ✅ executor `turn/interrupt` (`:2243`) | ✅ (`:2240`) `enqueue_message` (`:2276`) | ⚠️† `CODEX_HOME` isolation (`:1186/:1230`) | ✅ {none,minimal,low,med,high,xhigh} (`:2353`) | ❌ executor `approvalPolicy:"never"` (`:1427`) | ⚠️ resets thread (model in signature, `:2346`); effort resets (`:1450`) |

Also for claude-sdk: `supports_tool_boundary_interrupt`→✅ (`:1617`) — unique among in-scope.

### Native harnesses

| Native harness | interrupt | queue | subagents | reasoning effort | elicitation | mid-session model |
|---|---|---|---|---|---|---|
| **claude-native** | ✅ bridge `Escape` (`bridge:2530` via `app.py:_handle_claude_native_interrupt`) | ✅ (`exec:68`) tmux inject (`:72`) | ✅ `subagents/*.meta.json`→`external_subagent_start` (`fwd:217/1115`) | ✅ `/effort` slash-inject (`app.py:~11470`) — **⚠️ none/minimal skipped** (`:11521`) | ✅ PreToolUse + PermissionRequest HTTP hook (long-poll) | ✅ `/model` slash-inject + statusLine mirror (`app.py:11558`, `fwd:948`); next turn |
| **codex-native** | ✅ executor `turn/interrupt` (`exec:116`) + runner route (`app.py:10596`) | ✅ (`exec:64`) `turn/steer` (`:68`) | ✅ `source.subAgent.thread_spawn`→`external_codex_subagent_start` (`fwd:6079/4304`) | ✅ {openai set} via `thread/settings/update` (`exec:266/237`) | ✅ `codex-elicitation-request` hook (long-poll) | ✅ `thread/settings/update` (`exec:237`, `app.py:10664`); next turn |

**polly** has no row — its **brain** runs on **claude-sdk** and reads exactly as the claude-sdk
row. Its workers are `claude_code` (claude-native) and `codex` (codex-native), each reading as
its own native row (`examples/polly/config.yaml`).

† **codex (SDK) subagents** = private per-conversation `CODEX_HOME` (`tempfile.mkdtemp`,
`codex_executor.py:1202`) keeps child Codex sessions out of the user's history; this is process
isolation, not a declared subagent-mirror capability (unlike codex-*native*).

**Reasoning-effort source of truth** = `omnigent/reasoning_effort.py`:
`CLAUDE/ANTHROPIC = {low,medium,high,xhigh,max}` (`:13`),
`OPENAI/CODEX = {none,minimal,low,medium,high,xhigh}` (`:15`). Effort is selectable at session
start (NewChatDialog) and mid-session (`/effort <level>`).

---

## 2. SDK vs native taxonomy (who owns what)

| Concern | SDK (claude-sdk, codex) | Native (claude-native, codex-native) |
|---|---|---|
| Agent loop | **In-process** inside the harness subprocess (`run_turn` drives the vendor SDK) | **Resident vendor CLI** in tmux (claude) / behind a JSON-RPC app-server socket (codex), mirrored back |
| System prompt | **Omnigent** (`request.instructions` → `run_turn(system_prompt)`) | **Vendor** (the CLI's own settings); omni's system prompt is `del`'d (`claude_native_executor.py:123`) |
| Tool set | **Omnigent** (`request.tools` → `run_turn(tools)`; MCP bridge) | **Vendor** owns its tools; omni tools ignored (`tools` `del`'d). `sys_*` reachable via an MCP relay the CLI discovers |
| Transcript | **Omnigent owns 100%** (durable items from streamed events) | **Vendor store is source of truth**, mirrored: claude `~/.claude/projects/**.jsonl`; codex `$CODEX_HOME/sessions/**.jsonl` |
| Turn output | streamed `ExecutorEvent`s (`supports_streaming=True`) | forwarder polls vendor store → `external_*` events (`supports_streaming=False`) |
| Executor `run_turn` | full loop + tools + reasoning | inject latest user message, yield `TurnComplete(response=None)` immediately |
| `handles_tools_internally` | True (vendor loop) | True (native server runs its own tools) |

Wiring: each harness module builds an `ExecutorAdapter(executor_factory=…)`
(`claude_native_harness.py:29`, `codex_native_harness.py:28`, `claude_sdk_harness.py:309`); the
adapter (`_executor_adapter.py`) is the runner-facing FastAPI seam that runs the loop, emits
spans, and routes interrupt/queue/policy.

---

## 3. Model + reasoning-effort changes (session start AND mid-session, from web UI)

**Session start.** NewChatDialog passes model + effort (and for claude-native, permission mode).
The runner threads them in as `request.model_override` → `ExecutorConfig(model=…,
extra["reasoning_effort"]=…)` (`_executor_adapter.py:284`). For native harnesses the model/effort
is also baked into the spawn flags (claude appends `--model` `claude_native.py:4118-4120`;
effort persisted in metadata `:3845`).

**Mid-session (the key divergence).**
- **claude-sdk** — next-turn config. `cfg.model` read each turn (`:1910`); if it differs from the
  cached client's model, `client.set_model(model)` (`:1422`). Effort via `extra["reasoning_effort"]`
  → SDK `effort` (`:2011`). Applies on the **next** turn (not the running one).
- **codex (SDK)** — model is part of the session **signature**; changing it **closes the
  app-session and starts a fresh thread** (`:2346/2301/2304`). Effort applied via
  `thread/settings/update` and **deduped per thread**, reset on a new thread (`:1450/1464`).
- **claude-native — best-effort, two writers.** A web `/model` change → runner
  `_handle_claude_native_model_change` (`app.py:11558`) types `/model X` into tmux via
  `inject_slash_command(auto_confirm=True)`; the persisted `model_override` is the fallback on
  next spawn (the `--model` flag is baked at spawn, *not* re-read at turn boundaries — see the
  route docstring `:11565-11573`). Independently, the forwarder mirrors an **in-pane** `/model`
  switch back to `model_override` **every poll** (`_forward_model_from_status` `:948`) so
  model-gated policies don't lag a switch. Effort: `/effort L` slash-inject (`app.py:~11470`),
  **but only if L ∈ CLAUDE_EFFORTS** (`:11521`) — `none`/`minimal` are persist-only.
- **codex-native — RPC, persisted.** `_handle_codex_native_settings_update` (`app.py:10664`) →
  `thread/settings/update`; the executor itself also applies `_model_effort_overrides` before
  `turn/start` (`exec:266/237`). The TUI's own model/effort is mirrored into the server snapshot
  (`model_override`/`reasoning_effort`, `app.py:10754-10759`) so a settings-update can re-send it.

**Net:** SDK = next-turn config (codex actually re-threads); native = best-effort, applied on the
**next** turn, never the running one. claude-native's mechanism is keystroke-injection + statusLine
mirror; codex-native's is JSON-RPC.

---

## 4. Default model / provider resolution per harness (the chain)

Resolution chain (highest precedence first), resolved once at spec materialization
(`chat.py`): **CLI `--model`** → **`OMNIGENT_MODEL` env** (`chat.py:~123`) → **YAML
`executor.model`** → **`~/.omnigent/config.yaml` provider default** → **per-harness fallback**.
Ad-hoc specs with no executor block fall back to `_DEFAULT_AD_HOC_MODEL = "databricks-gpt-5-4"`
(`chat.py:99`). The resolved model rides as `request.model_override` → `cfg.model`.

Per-harness fallback when `cfg.model` is still None (inside the executor):
- **claude-sdk** — `cfg.model or self._model_override or _DATABRICKS_CLAUDE_DEFAULT_MODEL`
  (`:1910`); `_DATABRICKS_CLAUDE_DEFAULT_MODEL = "databricks-claude-opus-4-8"`
  (`databricks_config.py:85`) **only** on the Databricks-profile gateway path. ⚠️ **Bug #1128**:
  this Opus default fires when Sonnet was intended but the override arrived None.
- **codex** — `cfg.model or self._model_override or (_DATABRICKS_CODEX_DEFAULT_MODEL if
  databricks-profile else _OPENAI_CODEX_DEFAULT_MODEL)` (`:2334`); constants
  `databricks-gpt-5-5` (`:106`) / `gpt-5.4-mini` (`:100`).
- **claude-native** — the `claude` binary's own default unless omni resolved one (then `--model`);
  `use_claude_config=True` defers entirely to `~/.claude` config.
- **codex-native** — the Codex CLI's `~/.codex/config.toml` model/provider unless omni `--model`
  overrides (CODEX_HOME source resolver `codex_native.py:131`).

Provider/credentials: **claude-sdk/codex** resolve a gateway (Databricks AI-gateway base_url +
`databricks auth token` command, or a generic key/base_url gateway) or the vendor's built-in API
(`claude_sdk_harness.py` `HARNESS_CLAUDE_SDK_GATEWAY*`; `codex_executor.py:2171-2210`).
**claude-native/codex-native** default to the vendor's own auth (`~/.claude/.credentials.json`;
Codex `auth.json` — API-key or ChatGPT/OAuth, `codex_native.py:119-156`).

---

## 5. Propagating the user's OWN harness config into omni (#3)

- **claude-native — `use_claude_config`** (`claude_native.py:349`). Default `False` →
  `resolve_native_claude_config(spec=None)` provides an omni-managed isolated HOME + MCP relay
  (`:400`). `True` → **skips Databricks/ucode auth and uses the user's own `~/.claude/`
  configuration** (`:371-372`): credentials, `settings.json`, MCP servers, hooks. Strongest
  passthrough of any in-scope harness. ⚠️ a user `settings.json` model can conflict with omni's
  `--model`.
- **codex-native — `~/.codex/config.toml` + `auth.json`** inherited as the baseline via the
  `CODEX_HOME` source resolver (`codex_native.py:131`, `_codex_home_config_source_from_env`);
  omni runs in a *private* CODEX_HOME that maps back to the user's real home. Auth is the user's
  Codex `auth.json` (`:119-133`). omni `--model` / effort overrides layer on top via
  `thread/settings/update`.
- **SDK harnesses** receive config via `HARNESS_*` env vars from the workflow layer; they do not
  pass through the user's CLI dotfiles (claude-sdk explicitly strips `ANTHROPIC_API_KEY` to avoid
  bypassing subscription auth — `claude_sdk_harness.py:120-126`).

---

## 6. Harness switching mid-session

- `POST /sessions/{id}/switch-agent` (idle-only) swaps the bound agent; for **native** targets it
  clears `external_session_id` so the next turn **rebuilds the vendor transcript** (the native
  bridge re-binds and re-mirrors). Cross-family switches reset the model.
- Fork (`POST /sessions/{src}/fork`) clones the agent (optional harness switch); the native target
  rebuilds its transcript from the `FORK_CARRY_HISTORY` label. claude-native/codex-native both key
  their bridge to a `bridge_id` resolved from session labels, so `--resume`/fork land in the right
  pane/thread (`app.py:_claude_native_bridge_id_for_session`, used by every claude-native route).
- Polly switches *workers* by spawning the right sub-agent (claude_code/codex/pi), not by
  switching its own brain harness.

*(Server-side switch/fork mechanics are owned by the server SME; here the inner-layer fact is the
`external_session_id` reset → native transcript rebuild on next turn.)*

---

## 7. Elicitation / permission hooks per harness (which hooks; HOW verdicts return)

| Harness | hook(s) the harness must expose | how the verdict returns | NOT keystrokes? |
|---|---|---|---|
| **claude-sdk** | SDK `can_use_tool` callback + the adapter's `_elicitation_handler` / `_policy_evaluator` bridges (`_executor_adapter.py:307/314`) | server `type=approval` event → runner `pending_approvals` Future resolves the callback | ✅ event, no keystrokes |
| **codex (SDK)** | none at the executor (`approvalPolicy:"never"`, `:1427`) | n/a at the executor (no elicitation surfaced) | ✅ (no prompt) |
| **claude-native** | **PreToolUse + PermissionRequest** hooks (`native_policy_hook.py` reachable at `/policies/evaluate`) | **long-poll HTTP** — verdict carried in the held response body | ✅ long-poll, not keystrokes |
| **codex-native** | **`codex-elicitation-request`** hook (`codex_native_elicitation.py`) | **long-poll HTTP** | ✅ long-poll |

For the **in-scope** harnesses, verdicts return via a **long-poll HTTP hold** (claude-native /
codex-native) or an **approval event** (claude-sdk SDK callback) — **no keystroke emulation**.
(Other native harnesses out of scope use tmux-keystroke delivery; that is not how the in-scope set
works.) ⚠️ The native PreToolUse hook's static `policy_hook.json` token can expire → the hook
**fails CLOSED** (TOOL_CALL is a fail-closed phase) → tool calls die while chat survives
(`runner/app.py` snapshot path; PR #1439 — verify live). This is the single most important
elicitation-path reliability gap for native harnesses.

---

## 8. Reasoning: streamed vs persisted vs recomputed

- **Streamed (`ReasoningChunk`)** — claude-sdk emits `reasoning_started` / `reasoning_text`
  deltas live (`:2225/2266/2318`); codex emits `reasoning_text` from
  `item/reasoning/textDelta` (`:1663`). The workflow maps these onto
  `response.reasoning_text.delta` / `response.reasoning.started` SSE.
- **Recomputed (SDK)** — claude-sdk **does not persist thinking**: `compacted_messages` keeps
  only `content` blocks (`:2554-2558`), so reasoning is regenerated next turn / on resume.
- **Persisted (native)** — the vendor records its own reasoning in its transcript; the forwarder
  mirrors what the vendor exposes (claude-native mirrors reasoning items; codex-native mirrors
  app-server reasoning items). The vendor store is the durable record, not Omnigent's stream.
- **Compaction events**: claude-sdk emits `CompactionComplete` **with** `compacted_messages`
  (from the SDK's `get_session_messages()`, `:2540`) — note this contradicts the base
  `CompactionComplete` docstring claim that claude-sdk "cannot export." codex (SDK) emits **no**
  `CompactionComplete`. Native posts an `external_compaction_status` from the vendor's own compaction.

---

## 9. Quick reliability-gap recap (inner-layer)

- 🟠 **#1128 claude-sdk Opus billing** — `:1910` Databricks Opus-4.8 fallback on None model.
- ⚠️ **Native model override never affects the running turn** — next turn only (all four);
  codex-SDK actively tears down the thread.
- ⚠️ **claude-native `/effort none|minimal`** silently persist-only (not in CLAUDE_EFFORTS).
- ⚠️ **Native PreToolUse hook fail-closed on expired token** — chat works, tools blocked (PR #1439).
- 🟢 **Interrupt is NOT a gap** — claude-sdk/codex executor; claude-native bridge `Escape`;
  codex-native `turn/interrupt`.
- 📝 **codex-native subagents are real** (`external_codex_subagent_start`) — the §4 † footnote
  understates them; it applies to codex-SDK only.
