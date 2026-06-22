# Antigravity-native harness: RPC core rework

**Status:** Design (approved direction; pending implementation plan)
**Date:** 2026-06-22
**Supersedes:** the runtime core of PR #892 (antigravity-native). Periphery from #892 is reused.
**Source of truth for wire shapes:** live-verified against agy 1.0.10 — see memory `agy-rpc-interaction-bridge.md`.

## 1. Motivation

The current native harness (PR #892) drives the `agy` CLI through a tmux terminal with two indirect channels:

- **Read path** — tails agy's JSONL transcript (`~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl`) and mirrors steps into the session.
- **Write path** — types web turns into the agy TUI via tmux `send-keys`.

This works for plain text turns but has a **fragility class** and **functional gaps**:

1. **Out-of-order / non-contiguous `step_index`** in the JSONL forced a durable SET resume cursor, gap-free-prefix delivery, and audit gating — and still produced the **live double-render** (delta preview vs committed message) plus a **user-message duplication** (direct `/events` post vs the forwarder mirroring agy's `USER_INPUT`). Root-caused as inherent to the transcript-mirror approach; *predates* the out-of-order change (verified by running both revisions).
2. **No interactive-prompt support.** agy's `ask_question` (multi-select) and tool `request-review` approvals are TUI widgets with no web response path; the session sits `idle` while agy blocks.
3. **No real interrupt** (`interrupt_session` is stubbed to `False`).

A spike found agy exposes a **structured connect-RPC surface** that replaces all three channels cleanly. This design reworks the harness core onto that RPC, eliminating the transcript-mirror fragility and adding interactions + interrupt, while keeping the terminal-first UX (agy still runs in a tmux terminal).

## 2. Validated RPC surface

connect-RPC service `exa.language_server_pb.LanguageServerService`, TLS HTTP/2 on a loopback port, self-signed (`verify=False`), JSON accepted (`Content-Type: application/json`). All of the following are live-verified except where noted.

- **Identity:** `cascadeId == conversationId == brain-dir UUID` (one id). `GetCascadeId`/`GetCascadeStatus`/`ListCascadeIds` do **not** exist (404).
- **Port discovery:** reuse `omnigent/antigravity_native_rpc.py` — `discover_language_server_port(pid)` / `_candidate_agy_rpc_ports()` + `_conversation_matches` (Heartbeat → 200; `GetConversationMetadata` echoes `rootConversationId`).
- **Read / detect:**
  - `GetCascadeTrajectorySteps {cascadeId}` (unary) → `steps[]` — one-shot.
  - `StreamAgentStateUpdates {conversationId}` (connect server-stream, `application/connect+json`, 5-byte `[flag][BE-len]` framing) → first frame `update.mainTrajectoryUpdate.stepsUpdate.steps[]`, then long-polls — live.
  - (`StreamCascadeReactiveUpdates` is **deprecated** — returns `{"error":{"message":"reactive state is deprecated"}}`. Do not use.)
- **Step shape:** `status` ∈ `CORTEX_STEP_STATUS_{RUNNING,WAITING,DONE,ERROR}`; `type` ∈ `CORTEX_STEP_TYPE_{PLANNER_RESPONSE,RUN_COMMAND,...}`; `requestedInteraction` (`askQuestion` | `permission`) when `WAITING`; `runCommand.{commandLine, proposedCommandLine, cwd, exitCode, combinedOutput.full}`; `metadata.sourceTrajectoryStepInfo.{trajectoryId, stepIndex, cascadeId}`; `completedInteractions[].response` (echoes the delivered answer).
- **Answer interaction:** `HandleCascadeUserInteraction {cascadeId, interaction:{trajectoryId, stepIndex, <variant>}}` (unary) → `200 {}`.
  - **Question:** `interaction.askQuestion.responses:[{question:"<verbatim>", selectedOptionIds:["<option id>"]}]` (`writeInResponse` for write-ins). Option `id` is `"1".."N"`, not the text.
  - **Approval:** `interaction.permission.{allow: true|false}`. **No `approvalId`** — keyed solely by `trajectoryId+stepIndex` (the binary-tag `approvalInteraction.{approvalId,approve}` is a *different* approval kind, not the run_command path).
  - `trajectoryId` **and** `stepIndex` MUST be **inside** `interaction` (proto-JSON silently drops top-level extras).
- **Interrupt:** `CancelCascadeSteps` (and `ForceStopCascadeTree`).
- **Turn send (open):** `SendAgentMessage` records as a `SYSTEM_MESSAGE`, not `USER_INPUT` — so RPC turn-sending mis-attributes the user message. Turns therefore stay on tmux `send-keys` unless a proper user-turn RPC is found (see §7).

### 2.1 Timeout gotcha (critical)

A `WAITING` interaction **times out** server-side (→ `CORTEX_STEP_STATUS_ERROR`), after which agy **auto-retries with a fresh `WAITING` step at a higher `stepIndex`**. Omnigent elicitations wait on a human (potentially slow), so:

- The bridge must **re-read the freshest `WAITING` step at delivery time** — never trust the `trajectoryId/stepIndex` captured at detection.
- On a timeout-retry, the bridge must **re-surface / update** the elicitation against the new step.
- `HTTP 500 "input not registered for step N"` is **overloaded**: it means *either* a missing `trajectoryId` *or* a step that already timed out. Disambiguate by checking the step's `status` before treating it as a shape error.

## 3. Architecture

agy still runs in a runner-owned tmux terminal (terminal-first UX preserved). The transcript-tail reader and send-keys interaction path are replaced by RPC. New/changed units:

1. **RPC client** (`antigravity_native_rpc.py`, extended) — typed JSON wrappers: `get_trajectory_steps(port, cascade_id)`, `stream_agent_state_updates(port, conversation_id)`, `handle_user_interaction(port, cascade_id, interaction)`, `cancel_cascade_steps(port, cascade_id)`. Reuses the existing discovery/loopback/Heartbeat/GetConversationMetadata helpers.
2. **Step → item mapper** (pure, unit-testable) — trajectory `step` → omnigent conversation-item events (`message`, `function_call`, `function_call_output`, status edges). Replaces `step_to_events` over JSONL. Because RPC steps are structured and carry stable ids + explicit `status`, this drops the JSONL parsing, the `forwarded_steps` SET cursor, the gap-free-prefix logic, and the out-of-order handling — and **fixes the double-render** (one structured assistant item per step; no delta-vs-committed race). It also **skips `USER_INPUT` steps**: the user turn is already persisted by the direct `POST /events` input (authoritative), so re-emitting it from the trajectory is the source of the **user-message duplication** — the mapper must not mirror it (mirrors claude-native).
3. **Read driver** — polls `GetCascadeTrajectorySteps` (or consumes `StreamAgentStateUpdates`) and posts mapped items; dedup by `stepIndex`/step identity. Replaces the transcript-tail forwarder loop.
4. **Interaction bridge** — on a `WAITING` step, surface an omnigent elicitation (reuse the existing registry / `response.elicitation_request` SSE / `/resolve` / web UI). On resolve, run the **tight detect→deliver loop**: re-read the freshest `WAITING` step, build the `interaction` (`askQuestion` or `permission`), POST `HandleCascadeUserInteraction`; handle timeout/re-ask.
5. **Executor** — `run_turn` keeps tmux `send-keys` for turns (§7); `interrupt_session` → `CancelCascadeSteps` (real interrupt).
6. **Reused from #892 unchanged** — onboarding/agy-auth + Gemini provider, harness registration/aliases, the runner-owned terminal infra + auto-create + reattach fixes, the Docker agy-version pin, the ap-web picker/agent card, model catalog/override wiring.

## 4. Data flows

- **Assistant output (read):** read driver polls/streams steps → mapper → post `message`/`function_call`/`function_call_output` + status edges. Single structured item per step (no double-render).
- **User turn (write):** executor `send-keys` types the turn into agy's TUI (records as `USER_INPUT`). *(Unchanged from #892 until §7 resolves.)*
- **Interaction (question/approval):** read driver sees a `WAITING` step → interaction bridge surfaces an elicitation → user resolves in the web UI → bridge re-reads the freshest `WAITING` step and POSTs `HandleCascadeUserInteraction` → agy proceeds (step → `DONE`, `completedInteractions.response` echoes the answer).
- **Interrupt:** `interrupt_session` → `CancelCascadeSteps {cascadeId}`.

## 5. What is removed

- JSONL transcript tailing + partial-line buffering + UTF-8 hold-back.
- The durable `forwarded_steps` SET cursor, gap-free-prefix delivery, out-of-order suppression, the `<=`-floor legacy materialization.
- The delta (`output_text_delta`) + committed-message double-emission (source of the live double-render).
- The forwarder mirroring of `USER_INPUT` that duplicated the direct `/events` user post.
- tmux `send-keys` for *interactions* (kept only for turns, pending §7).

## 6. What is reused (from #892)

Onboarding/auth, Gemini provider config, harness registration/aliases, runner-owned terminal + auto-create + the reattach/no-double-forward fixes, the Docker `AGY_EXPECTED_VERSION` pin, the ap-web Antigravity picker/agent card, model catalog/override/effort wiring. The three review fixes already committed on the branch (`1cd8f5aa`, `874f8f5c`, `708ee883`) stay relevant (terminal/launch infra + test hygiene).

## 7. Open questions (resolve in the plan)

1. **Turn send.** Keep tmux `send-keys` (proven, records `USER_INPUT`) vs. find a proper user-turn RPC (the obvious `SendAgentMessage` mis-records as `SYSTEM_MESSAGE`). Default: keep send-keys; small spike to look for a user-turn RPC (e.g. a queued-user-input method).
2. **Poll vs stream for read.** `StreamAgentStateUpdates` (live, lower latency, connect-stream framing) vs `GetCascadeTrajectorySteps` polling (simpler). Likely stream with poll fallback.
3. **Elicitation ↔ agy-timeout reconciliation.** Concrete policy for re-surfacing on timeout-retry and for the deny/cancel path (`permission.allow=false`; multi-select; write-in).
4. **#892 packaging.** Evolve the existing branch (reuse periphery + fixes) vs a fresh PR cherry-picking the periphery. Lean: evolve the branch; keep it draft until the RPC core lands.

## 8. Testing

- **Unit:** step→item mapper from recorded RPC step fixtures (question, approval, run_command, planner, tool-output); interaction-builder shapes.
- **Integration (live agy):** question round-trip, approval round-trip, interrupt — mirroring the spikes (assert `200 {}`, step→`DONE`, `completedInteractions.response`, trajectory growth / command output).
- **Timeout handling:** simulate a timed-out `WAITING` step (status `ERROR`) → bridge re-reads and delivers to the retry step; assert no spurious 500 propagation.
- **Parity / regression:** adapt the existing native-harness suites; confirm the double-render and user-dup are gone (persisted + live single render).

## 9. Risks

- agy RPC is undocumented/unstable across versions — mitigated by the Docker version pin and the version-gated build.
- Port discovery timing (agy must be up + bound) — reuse the existing discovery + retry.
- The timeout gotcha (§2.1) is the main correctness-sensitive area — covered by the tight detect→deliver loop + tests.
- Terminal-first UX: agy still runs in the tmux terminal, so the TUI and RPC both drive the same cascade (verified compatible — TUI and RPC interaction delivery coexist).
