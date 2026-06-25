# Goose-native gap plan

Status: **design / proposed** · Owner: harness · Companion harness: headless `goose` (ACP)

This document specifies how to close the open capability gaps in the
**goose-native** harness (the tmux-TUI mirror added in #955), measured against
the `harness-integration-guide` skill's native-harness checklist. It is the
output of a source-grounded gap analysis across both the omnigent codebase and
the upstream goose source (`github.com/aaif-goose/goose`, goose 1.38).

The companion **headless `goose`** harness (ACP, on main) is referenced
throughout: it drives `goose acp` and rides the structured ACP stream, so it
already solves several gaps that are architecturally hard for the TUI mirror.
Where a gap is capped on native, the headless harness is the recommended path.

---

## 1. Architecture recap (why some gaps are easy and some are hard)

goose-native is an **observe-and-relay** harness:

- **Launch** — the runner spawns `goose session --name <conv-id>` in a runner-owned
  tmux pane (`runner/app.py` `_auto_create_goose_terminal`; env from
  `goose_native_bridge.build_goose_native_spawn_env`).
- **Web → TUI** — user turns are injected into the pane via bracketed-paste
  (`inner/goose_native_executor.py`).
- **TUI → Web** — `goose_native_forwarder.py` tails goose's SQLite store
  (`~/.local/share/goose/sessions/sessions.db`) and mirrors **completed** messages
  back as `external_conversation_item` events (poll cadence 0.4s).
- **Approvals** — a goose `PreToolUse` plugin hook
  (`inner/goose_policy_hook.py`) evaluates each tool call against Omnigent policy
  and blocks on DENY; ASK surfaces a web card via `/policies/evaluate` (see §3).

The store flushes **one row per completed step** (no token deltas), and goose's
TUI exposes **no structured side-channel** for permissions, reasoning, or
compaction — those are rendered to the terminal, not emitted. This is the root
cause of the hard gaps (streaming, compaction). Conversely, anything goose
**persists** to the store (text, tool calls, **thinking**, cost, usage) is
recoverable by the forwarder, which is why most gaps are in fact fillable.

The headless `goose` harness instead consumes `goose acp`: structured
`session/update` notifications (`agent_message_chunk`, `tool_call`,
`usage_update`, `AgentThoughtChunk`) plus structured `session/request_permission`.
That is why streaming/policy/compaction are clean there.

---

## 2. Gap scoreboard (verified)

| # | Gap | Verified state on native | Plan |
|---|---|---|---|
| 1 | **Omnigent policies** | ✗ — no Omnigent eval; goose's own `GOOSE_MODE` gates | **§3 — fill on native** (the hard one) |
| 2 | **Model override** | ✗ — goose owns provider/model via `goose configure` | §4.1 — **skip (decided)**; steer to headless |
| 3 | **Reasoning** (P1) | ✗ — thinking *is* persisted, forwarder drops it | §4.2 — fill |
| 4 | **Cost tracking** (P1) | ✗ — `accumulated_cost`/tokens in store, not forwarded | §4.3 — fill |
| 5 | **Resume / fork** (fork P1) | resume ✓; fork ✗ | §4.4 — fill |
| 6 | **Omnigent MCP** | ✗ by design | §5.1 — **fill (in scope, decided)** |
| 7 | **Session-cmd sync** | ✗ | §5.2 — partial (Tier 2) |
| 8 | **Elicitation (web)** | ✓ but mirrors goose's own decision | folded into §3 |
| 9 | **Images** | input ✓ (`[Attached:]`); output N/A | §5.3 — no-op |
| 10 | **Auth** | ✓ (`goose info -v`) | done |
| 11 | **Interrupt** | ✓ (tmux) | done |
| 12 | **Bidirectional sync** | ✓ | done |
| 13 | **Streaming** | complete-only | **§6 — out of scope** (use headless) |
| 14 | **Compaction** | ✗ — goose emits no signal | **§6 — out of scope** (use headless) |

---

## 3. Omnigent policies on goose-native (the centerpiece)

**Requirement** (from the skill): the harness must enforce Omnigent's three
verdicts — ALLOW / ASK / DENY — at the tool-call checkpoint, surfacing ASK as a
web approval card and blocking DENY before the tool runs.

**Correction (live testing).** An earlier draft of this plan claimed "goose has
no tool-hook system" and built a brittle `cliclack`-screen-scrape mirror around
`GOOSE_MODE=approve`. **That was wrong.** goose ships a full **Claude-Code-style
hook system** (`crates/goose/src/hooks/mod.rs`): events include `PreToolUse`,
`PostToolUse`, `UserPromptSubmit`, `Stop`, …; hook commands receive the event
JSON on stdin and **block** by printing `{"decision":"block","reason":"…"}`.
`PreToolUse` is dispatched **blocking** (`emit_blocking`, `agent.rs:1066` →
`HookDecision::Deny` skips the tool). So the right mechanism is goose's own hook
— the same path claude-/hermes-native use — not screen-scraping. The scrape
mirror has been **removed**.

### 3.1 Design — a goose `PreToolUse` plugin hook

```
GOOSE_MODE=auto  → goose runs tools with NO in-TUI prompt; the hook is the gate
        │
   goose fires PreToolUse (blocking) before EVERY tool, from web OR terminal turns
        │
   plugin hook: omnigent.inner.goose_policy_hook  (stdin = {event, tool_name, tool_input})
        │
   POST /v1/sessions/{id}/policies/evaluate   (PHASE_TOOL_CALL)
        │
   ┌─────────────┬───────────────────────────┬──────────────────┐
 ALLOW/UNSPEC    ASK (engine holds gate,      DENY            (net error)
   │             renders web card, waits)      │                  │
 print {}        returns hard ALLOW/DENY      print block      print block
 (allow)         → print {} or block          (deny)           (fail-closed)
```

- **Real enforcement, both input sources.** `PreToolUse` fires inside goose's
  own loop, so it gates a tool whether the turn came from the web composer **or**
  was typed into the embedded terminal. `{"decision":"block"}` truly stops the
  call — no scraping, no `approve`-mode prompt flicker.
- **ASK → web card.** `/policies/evaluate` resolves ASK server-side
  (`_hold_native_ask_gate`, `sessions.py:3907`): it publishes the approval card,
  parks the hook's HTTP request until the human answers, and returns a hard
  ALLOW/DENY. The hook is synchronous (read-timeout 1 day), so this Just Works.
- **Registration as a project-scope plugin.** goose discovers hooks from a
  plugin's `<plugins_dir>/<name>/hooks/hooks.json`, where one `plugins_dir` is
  `<project_root>/.agents/plugins` and `project_root` = goose's cwd = the session
  workspace. So the runner writes the plugin to
  `<workspace>/.agents/plugins/omnigent-policy/` and best-effort git-excludes it
  (`.git/info/exclude`) so it never shows in `git status`. goose stays on its
  **real home**, so credentials resolve via the OS keyring exactly as the user's
  own `goose`. **An isolated `GOOSE_PATH_ROOT` home was tried first and removed:
  it broke startup — goose couldn't read the keychain key in the rebased home and
  died, so the terminal never went ready (60s timeout). Live-confirmed on macOS.**
- **Fail-closed.** Network error / retry exhaustion → the hook prints
  `block` (deny). A truly-unexpected hook crash fails open (goose proceeds) — the
  same contract as `hermes_policy_hook`.

### 3.2 What changed

- `inner/goose_policy_hook.py` (new) — the `PreToolUse` entrypoint; near-copy of
  `inner/hermes_policy_hook.py`. Reads `_OMNIGENT_SERVER_URL` /
  `_OMNIGENT_SESSION_ID` from the inherited env, POSTs `PHASE_TOOL_CALL` via
  `native_policy_hook.post_evaluate_with_retry`, maps DENY/ASK → block.
- `goose_native_bridge.py` — `write_goose_policy_plugin(workspace)` writes the
  project-scope `hooks.json` + git-excludes it; `clear_goose_policy_plugin` for
  teardown. (No isolated home / sessions repoint — goose uses its real home.)
- `runner/app.py` — `GOOSE_MODE=auto`; set `_OMNIGENT_*`; write the project-scope
  plugin into the workspace before launch. The pollers read goose's default
  sessions.db. The cliclack-scrape mirror (`goose_native_permissions.py`) and its
  `approve`-mode are **removed**.
- `claude_native_bridge.py` — add the goose-native bridge root to the `serve-mcp`
  allowlist (`_trusted_parent_for_bridge_dir`) so the Omnigent MCP extension can
  boot (found in live testing).

### 3.3 Honest residual limits

- **Text-only TERMINAL turns can't be *request-phase* blocked.** goose's
  `UserPromptSubmit` hook is dispatched **non-blocking** (`.emit()`, not
  `emit_blocking` — `agent.rs:1582,1906`), so unlike Claude Code's, it cannot
  veto a turn before the model runs. A turn typed into the embedded terminal that
  produces only text (no tool call) therefore bypasses request-phase gates
  (input policies, `cost_budget`'s request check). Tool calls in such a turn
  **are** gated (`PreToolUse`), and web-composer turns are gated server-side
  (`_evaluate_input_policy`). So cost-budget enforcement holds on web turns and
  on any tool-bearing turn, but a text-only terminal turn runs ungated. For
  guaranteed turn-level enforcement, the headless `goose` harness owns the turn
  loop. Two related cost-budget sharp edges: (a) the builtin `cost_budget`
  `max_cost_usd` is a *downgrade gate* — only DENYs on an `expensive_models`
  model (defaults opus/gpt-5/…), so a bare cap never hard-stops; (b) the engine
  sees the *spec* model, not goose's `goose configure` model, so expensive-model
  matching needs goose's live model surfaced (`external_model_change` — not yet
  wired). **Follow-up candidates:** a non-blocking `UserPromptSubmit` *audit*
  hook, and surfacing goose's live model.
- **Tool-*result* checkpoint is audit-only.** No goose post-exec hook can block a
  result already returned to the model. `goose_native_audit.py` evaluates
  `PHASE_TOOL_RESULT` for the record (side-effect-free — that phase doesn't park
  a gate) and logs a non-allow verdict. The headless harness enforces both
  checkpoints.

---

## 4. Tier 1 — P1 + the launch bug (all native, all independently shippable)

### 4.1 Model override — skipped for native (decided)

**Decision: goose-native does NOT support an Omnigent model override.** goose's
provider *and* model live in the user's `goose configure` keyring/config, and
goose has no `--model` flag — so Omnigent setting `GOOSE_MODEL` can't reliably
pick a model valid for the user's configured provider (Omnigent can't know it).
Forcing it risks an invalid model that breaks the turn. Mid-session switch is
impossible regardless (goose reads `GOOSE_MODEL` only at launch — no ACP
`set_model`, no `/model` command).

- **Implementation:** `harness_supports_model_override("goose-native")` now
  returns `False` (`model_override.py`), so the web picker doesn't offer a model
  for goose-native and the dispatch-time gate rejects a stray persisted value
  rather than silently dropping it. goose-native uses whatever `goose configure`
  set.
- **Steer:** users who need per-session model switching should pick the headless
  `goose` harness, which threads the model via `HARNESS_GOOSE_MODEL`.

### 4.2 Reasoning forwarding (P1) — the corrected finding

goose **persists** reasoning: `MessageContent::Thinking` serializes as
`{"type":"thinking","thinking":"…"}` into `content_json` (`message.rs:279`,
`:41`) and also streams over ACP as `AgentThoughtChunk` (`acp/server.rs:1350`).
The native forwarder's `_content_text` only extracts `{"type":"text"}` and
treats thinking-only turns as "reasoning-only turn with no prose" → **drops
them** (`goose_native_forwarder.py:196,255`).

- **Fix:** split content extraction so `{"type":"thinking"}` parts emit a
  reasoning event (mirror codex-native's `output_reasoning`) instead of being
  discarded. Redacted thinking → a redacted marker.
- Effort: ~1–2 days incl. tests. Risk: low. **Lowest-risk P1 — do first.**

### 4.3 Cost tracking (P1)

goose persists `accumulated_cost` + `accumulated_input/output_tokens` in the
store, and ACP carries `usage_update.accumulated_cost`.

- **Fix:** new `goose_native_usage.py` poller modeled on
  `cursor_native_usage.py` — read the store, POST `external_session_usage`,
  dedup by message id, handle the **fork-resets-accumulators** edge case
  (accumulated_* restart in a forked session).
- Effort: ~1–1.5 days incl. tests. Risk: low.

### 4.4 Resume / fork (fork is P1)

Resume already works (live reattach + cold relaunch `goose session --name <id>`,
which reloads prior messages from the store). **Fork is not wired.** Both a CLI
`--fork` and an ACP `ForkSessionRequest` handler exist upstream
(`acp/server/fork_session.rs`, `dispatch.rs:375`).

- **Fix (easy path):** Omnigent SDK fork already works (`chat.py:1663`); pass the
  forked conv-id to goose-native and relaunch `goose session --name <new-id>` —
  goose loads the copied history from its own store. No fork-preamble needed
  (unlike cursor, whose history is server-side).
- **Optional parity:** cursor-style fork-preamble for explicit text continuity.
- Wire a `fork_session_id` param through `run_goose_native` →
  `_prepare_goose_terminal_via_daemon`.
- Effort: ~2 days easy path. Risk: low–medium.

---

## 5. Tier 2 — native polish

### 5.1 Omnigent MCP — **in scope (decided)**

goose loads MCP servers via `--with-extension <cmd>` (stdio),
`--with-streamable-http-extension <url>` (HTTP) (`cli.rs:163,172`),
`config.yaml`, or ACP `extensions/add`. Today the runner writes none (by design).

- **Approach:** `--with-streamable-http-extension <omnigent-relay-url>` at
  launch — no user-config mutation, per-session, points goose at the same
  serve-mcp relay the other native harnesses use
  (`claude_native_bridge serve-mcp`). The `config.yaml`-write alternative
  mutates user state and needs a consent guard, so prefer the launch flag.
- **Synergy with §3:** goose gates extension tools with `GOOSE_MODE`, so the
  Omnigent MCP tools flow through the **same §3 policy path** as any other tool
  — one enforcement point covers both goose builtins and Omnigent MCP. (We do
  *not* want to double-evaluate: `mcp__omnigent__*` tools are already
  policy-checked on the relay path, and `hook_payload_to_evaluation_request`
  skips them — `native_policy_hook.py:108` — so the §3 gate must apply the same
  skip and let the relay gate own those.)
- Decided to fill on native despite the headless harness also having MCP.
- Effort: ~2–3 days. Risk: medium (goose HTTP-extension maturity).

### 5.2 In-harness session-cmd sync

goose advertises `available_commands` over ACP (`compact`, `clear`, `prompts`,
`skills`, …; `acp/response_builder.rs:364`). `/fork` and `/resume` are *not*
in-session goose commands — they are CLI relaunch (→ §4.4).

- **Fill:** wire web `/clear` → inject into the pane; surface goose's command
  list through the #1168 composer-discovery path.
- Effort: ~2 days. Risk: low–medium.

### 5.3 Images

Input works (materialized to disk + `[Attached: <path>]` marker). goose does not
emit image **output**, so there is nothing to mirror. **No-op** until goose
gains image output.

---

## 6. Out of scope (use the headless `goose` harness)

Per decision: **skip streaming and compaction on native.**

- **Token streaming** — the store flushes only completed steps; there are no
  partial deltas to tail. Token streaming requires the ACP stream → headless
  (already streams, verified). The native live-streaming surface is the
  terminal.
- **Compaction** — goose emits no structured signal (only a user-visible
  "Compaction complete" string; the `StatusMessage::Notice` enum has no emitter).
  Usage-delta heuristics are false-positive-prone. Best long-term fix is an
  upstream goose signal; until then neither path surfaces it reliably.

Recommendation: document that **policy/streaming/compaction-sensitive users
should select the headless `goose` harness**, which solves all three natively.

---

## 7. Sequencing, effort, risk

**PR scope (decided):** policies (§3) **and** Omnigent MCP (§5.1) ship together
with the Tier-1 gap-fills (§4) as **one PR** — the user asked for policies in the
same PR, and MCP shares the §3 enforcement path, so they are one coherent unit.
`/clear` + command-discovery and resume-hardening (§5.2) may follow as a small
Tier-2 PR if this one grows too large to review.

| Group | Items | Effort | Risk |
|---|---|---|---|
| **Policy** (§3) | goose `PreToolUse` project-scope plugin hook → `/policies/evaluate`; real home (keychain auth); tests | done | low (native hook) |
| **MCP** (§5.1) | `--with-streamable-http-extension` at launch; `mcp__omnigent__*` skip in the §3 gate | ~2–3 days | medium |
| **Tier-1** (§4) | reasoning → model-launch → cost → fork | ~5–6 days | low–medium |

Suggested build order within the PR: **§3 policy first** (the explicit
requirement; recasts the existing elicitation mirror), then §4.2 reasoning
(lowest-risk P1), then §5.1 MCP (rides the §3 gate), then §4.1/§4.3/§4.4.

## 8. Test plan

- **Policy (§3):** unit — verdict→keystroke mapping incl. fail-closed;
  pending-toolRequest reader; `smart_approve`→`approve` env assertion. E2E
  (opt-in, like `test_goose_native_cli_e2e.py`) — configure an ASK policy on a
  tool, drive a turn, assert the web card appears and the verdict gates goose;
  configure a DENY policy, assert the tool is blocked.
- **Tier 1:** unit per item (thinking extraction; `GOOSE_MODEL` threading;
  usage dedup + fork-reset; fork relaunch arg-building). Mock-LLM happy path.
- All work on branch `goose-native-gaps`; keep `package-lock.json` / `uv.lock`
  clean (no proxy leak).

## 9. Decisions (resolved 2026-06-25)

1. **MCP on native (§5.1):** **yes — in scope.** Fill via
   `--with-streamable-http-extension`; tools ride the §3 policy gate (with the
   `mcp__omnigent__*` skip to avoid double-evaluation).
2. **Policy PR scope (§7):** **policies ship in the same PR** as the MCP +
   Tier-1 gap-fills, not as a separate phased PR.
3. **`approve`-mode chattiness (§3.2):** **include the no-policy fast-path in
   v1** — `approve` mode prompts on every tool, so the per-tool round-trip is
   only paid when policies actually exist.
4. **Tool-result audit (§3.3):** **in scope, scheduled last** — implement the
   non-blocking post-hoc `PHASE_TOOL_RESULT` audit evaluation after everything
   else works.
