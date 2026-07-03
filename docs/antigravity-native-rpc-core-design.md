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
6. **Reused from #892 unchanged** — onboarding/agy-auth + Gemini provider, harness registration/aliases, the runner-owned terminal infra + auto-create + reattach fixes, the Docker agy-version pin, the web picker/agent card, model catalog/override wiring.

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

Onboarding/auth, Gemini provider config, harness registration/aliases, runner-owned terminal + auto-create + the reattach/no-double-forward fixes, the Docker `AGY_EXPECTED_VERSION` pin, the web Antigravity picker/agent card, model catalog/override/effort wiring. The three review fixes already committed on the branch (`1cd8f5aa`, `874f8f5c`, `708ee883`) stay relevant (terminal/launch infra + test hygiene).

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

## 10. Phase 2 — Full RPC parity with codex/claude (live-verified, agy 1.0.10)

Spikes (`/tmp/agy-turnsend-spike.md`, `/tmp/agy-parity-feasibility.md`; memory `agy-rpc-interaction-bridge`, `agy-rpc-parity-streaming`) confirmed agy exposes everything needed to reach full codex/claude parity over RPC. This resolves §7 open question 1 (turn-send) and adds streaming + telemetry. **All shapes version-volatile — resolve model enums at runtime, never hardcode.**

### 10.1 Turn-send (resolves §7-Q1; replaces tmux send-keys)
`SendUserCascadeMessage {cascadeId, items:[{text:<turn>}], cascadeConfig:{plannerConfig:{planModel:<MODEL enum>}}}` → `200 {}`; records `CORTEX_STEP_TYPE_USER_INPUT` + `metadata.source==CORTEX_STEP_SOURCE_USER_EXPLICIT` + `userInput.userResponse==<text>` (byte-for-byte what the reader keys on). Gotchas: text in `items[].text` (NOT flat `message`); model REQUIRED per-turn in `cascadeConfig.plannerConfig` (omit → ERROR "neither PlanModel nor RequestedModel specified"). Resolve `planModel` at runtime via `GetAvailableModels {}` or by echoing the read-side `userInput.userConfig.plannerConfig.requestedModel.model`. `SendAgentMessage`=SYSTEM_MESSAGE (wrong); queue methods retired.

### 10.2 Streaming deltas (live typing — output_text_delta parity)
`StreamAgentStateUpdates` connect server-stream. Request body MUST be connect-enveloped: `[flag=0x00][BE-uint32 len][{"conversationId":<conv>}]`, header `Content-Type: application/connect+json`. Response frames `[flag][BE-len][json]`: flag 0=data, flag 2=trailer.
- Partial text path: `update.mainTrajectoryUpdate.stepsUpdate.steps[]` where `type==PLANNER_RESPONSE`, at **`plannerResponse.modifiedResponse`** — GROWS across frames while `status==CORTEX_STEP_STATUS_GENERATING`. `plannerResponse.thinking` streams first (reasoning). **Trap:** `plannerResponse.response` is ABSENT during generation, populated only on the DONE commit (where `response==modifiedResponse`). (This validates Task 4's `modifiedResponse` preference.)
- Discriminator = step `status`: GENERATING ⇒ partial (emit `output_text_delta` = `modifiedResponse` minus last forwarded prefix, keyed by `metadata.sourceTrajectoryStepInfo.stepIndex`); DONE ⇒ final committed `message` (from `response`; `metadata.modelUsage` set). Frames are CUMULATIVE snapshots → harness owns prefix-diffing. On connect, a snapshot of prior steps replays (DONE) → dedup against already-forwarded committed steps.
- Reconciler: deltas precede the committed item (flush-barrier style, like codex) so the SPA retires the live preview rather than double-rendering. Keep the unary `GetCascadeTrajectorySteps` poll as snapshot/reconnect fallback (same `plannerResponse` shape).

### 10.3 Token usage (external_session_usage parity)
Per model call: `step.metadata.modelUsage = {model, inputTokens, outputTokens, thinkingOutputTokens, responseOutputTokens, cacheReadTokens}` (string ints; output=thinking+response). Cumulative context estimate (monotonic): `trajectory.generatorMetadata[].chatModel.chatStartMetadata.contextWindowMetadata.estimatedTokensUsed`. No top-level usage RPC. Per-turn = sum `modelUsage` over the turn's steps; emit as each PLANNER step hits DONE.

### 10.4 Model / effort (external_model_change parity)
Current model per-turn: `step.userInput.userConfig.plannerConfig.requestedModel.model` (+ `step.metadata.{generatorModel, modelUsage.model}`). Enum→displayName via `GetAvailableModels {}` → `response.models[key].{model, displayName, recommended, supportsThinking, thinkingBudget}`. Effort is NOT separate — it's encoded in the model enum (Gemini: M20/M132/M187 = Medium/High/Low) or a Thinking variant (Claude: M26 = Opus 4.6 Thinking). Detect change by diffing the per-turn enum vs previous (no "model-changed" step). LIVE-CONFIRMED: TUI /model switch flips the next turn's `requestedModel`/`generatorModel`.

### 10.5 New-conversation rotation (/clear → session rotation parity)
TUI `/clear` creates a NEW cascadeId/brain-dir UUID immediately; old conv retained (still reachable), not deleted. Detect via `GetAllCascadeTrajectories {}` → `trajectorySummaries{<conv>:{status, stepCount, trajectoryId, lastUserInputTime, lastModifiedTime, lastUserInputStepIndex}}` (a new convId key, or the bound conv going stale while a sibling advances); each stream frame's `update.conversationId` also names the active conv. On rotation, re-discover the active conv (max `lastUserInputTime`) and rotate the omnigent session (mirrors codex `thread/started`). `StartCascade` is the programmatic cold-start (not needed for human /clear).

### 10.6 Phase-2 task plan
- **T-A** RPC client: `send_user_cascade_message` + model resolution (`get_available_models`, resolve current enum). **T-B** executor `run_turn` → RPC turn-send (drop send-keys).
- **T-C** RPC client: `stream_agent_state_updates` (connect-stream framing). **T-D** reader streaming mode: deltas (modifiedResponse prefix-diff + thinking) + DONE commit + snapshot dedup + poll fallback.
- **T-E** usage events, **T-F** model-change events, **T-G** /clear rotation — reader additions (read from step.metadata / GetAllCascadeTrajectories).
- Integrates with Task 10 (interrupt+T-B), Task 11 (runner wiring of streaming reader+bridge+rotation), Task 13 (e2e parity: streaming single-render, usage, model-change, rotation, turn-send).
