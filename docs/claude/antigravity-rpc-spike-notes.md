# Antigravity-native RPC core — spike notes (Task 1)

**Date:** 2026-06-22
**agy version:** 1.0.10 (`/Users/bryanli/.local/bin/agy --version` → `1.0.10`)
**Host:** standalone attended agy in a dedicated tmux session `agy-spike` (no `--dangerously-skip-permissions`), launched with `HOME=/Users/bryanli`. NOT the `:6767` omnigent; fully isolated from the `rdv-*` sessions.
**Conversation captured:** `2399249c-4a48-40f1-bf3b-4c6e5d3a5a0e` (= cascadeId = brain-dir UUID).
**RPC port discovered:** `53485` via `discover_language_server_port(17262)` (PID 17262), confirmed by `_conversation_matches(port, conv) → True`.

This task adds **fixtures + this notes doc only** (no production code). It records, with live evidence:
1. the distinct `GetCascadeTrajectorySteps` step shapes Tasks 4/5 will map and assert on (saved under `tests/fixtures/antigravity/steps/`);
2. the **turn-send** verdict (Step 3);
3. the **read-mode** verdict + latency/reliability (Step 4).

All shapes here were captured **verbatim from the live RPC** (re-serialized pretty-printed; no content edits) unless explicitly labelled synthesized.

---

## 1. Fixtures captured

All fixtures are the single `steps[]` element as returned by
`POST .../LanguageServerService/GetCascadeTrajectorySteps` with request body
`{"cascadeId": "<conv>"}` (Content-Type `application/json`, `verify=False`).

| Fixture file | `type` | `status` | Live? | What Tasks 4/5 assert on |
|---|---|---|---|---|
| `user_input.json` | `CORTEX_STEP_TYPE_USER_INPUT` | `DONE` | live | `userInput.userResponse`, `userInput.items[].text`, `metadata.source = CORTEX_STEP_SOURCE_USER_EXPLICIT`. **The mapper SKIPS this** (user turn already persisted by `/events`). NB: `metadata.sourceTrajectoryStepInfo.stepIndex` is **absent** here (step 0 → proto omits the zero default; treat missing as 0). |
| `conversation_history.json` | `CORTEX_STEP_TYPE_CONVERSATION_HISTORY` | `DONE` | live | system step, `conversationHistory: {}` — mapper skips (non-renderable). |
| `planner_response_text.json` | `CORTEX_STEP_TYPE_PLANNER_RESPONSE` | `DONE` | live | `plannerResponse.response` + `plannerResponse.modifiedResponse` (assistant text), `plannerResponse.messageId`, `plannerResponse.stopReason`. Carries a large `plannerResponse.thinkingSignature` (opaque; ignore). → `message` item. |
| `planner_response_tool_call_ask_question.json` | `CORTEX_STEP_TYPE_PLANNER_RESPONSE` | `DONE` | live | `plannerResponse.toolCalls[].{id, name:"ask_question", argumentsJson}` (+ optional `plannerResponse.thinking`). The tool-call carrier → `function_call` item. |
| `planner_response_tool_call_run_command.json` | `CORTEX_STEP_TYPE_PLANNER_RESPONSE` | `DONE` | live | `plannerResponse.toolCalls[].{id, name:"run_command", argumentsJson}` — distinct tool-call variant. |
| `run_command_waiting.json` | `CORTEX_STEP_TYPE_RUN_COMMAND` | `WAITING` | live | **permission-pending shape**: `requestedInteraction.permission.{resource.{action:"command", target:"pwd"}, persistSuggestionType, suggestedPersistPattern, actionDescription}`; `runCommand.{commandLine, proposedCommandLine, cwd, blocking, waitMsBeforeAsync}` (no `exitCode` yet); `metadata.sourceTrajectoryStepInfo.{trajectoryId, stepIndex}`. |
| `run_command_done.json` | `CORTEX_STEP_TYPE_RUN_COMMAND` | `DONE` | live | `runCommand.exitCode` (0), `runCommand.combinedOutput.full`, `runCommand.{commandLine, proposedCommandLine, cwd}`; `completedInteractions[].request.permission.resource.{action,target}` + `completedInteractions[].response = {trajectoryId, stepIndex, permission:{allow:true}}`. |
| `ask_question_waiting.json` | `CORTEX_STEP_TYPE_ASK_QUESTION` | `WAITING` | live | **ask-question-pending shape**: `requestedInteraction.askQuestion.questions[].{question, options[].{id,text}}` (option `id` = `"1".."N"`); also `metadata.toolCall.{id,name:"ask_question",argumentsJson,originalName}` and a top-level `askQuestion` block (same content). `metadata.sourceTrajectoryStepInfo.{trajectoryId, stepIndex, metadataIndex}`. |
| `ask_question_done.json` | `CORTEX_STEP_TYPE_ASK_QUESTION` | `DONE` | live | answered shape: `completedInteractions[].response.askQuestion.responses[].{question, selectedOptionIds:["4"]}`. |
| `list_directory_done.json` | `CORTEX_STEP_TYPE_LIST_DIRECTORY` | `DONE` | live | tool-result step: `listDirectory.{directoryPathUri, results}` — another distinct tool step the mapper must classify. |
| `checkpoint.json` | `CORTEX_STEP_TYPE_CHECKPOINT` | `DONE` | live | system step (`checkpoint` block, `metadata.modelUsage`/`retryInfos`) — mapper skips. |
| `run_command_error.json` | `CORTEX_STEP_TYPE_RUN_COMMAND` | `ERROR` | **synthesized** (see §1.1) | timed-out `WAITING`→`ERROR` permission step. `metadata.internalMetadata.statusTransitions` ends with the `WAITING`→`ERROR` flip; `requestedInteraction.permission` still present. Carries a `_fixtureProvenance` marker. Models the §2.1 timeout gotcha. |

**Field-path cheatsheet for Tasks 4/5** (paths are stable across every step):
- `step.type`, `step.status`
- `step.metadata.sourceTrajectoryStepInfo.{trajectoryId, stepIndex, cascadeId}` (`stepIndex` omitted when 0)
- `step.metadata.source` (`CORTEX_STEP_SOURCE_{USER_EXPLICIT, MODEL, SYSTEM}`)
- `step.requestedInteraction.{askQuestion | permission}` (only when `WAITING`)
- `step.requestedInteraction.askQuestion.questions[].options[].{id, text}`
- `step.requestedInteraction.permission.{resource.{action,target}, actionDescription, suggestedPersistPattern, persistSuggestionType}`
- `step.plannerResponse.{response, modifiedResponse, messageId, stopReason, toolCalls[].{id,name,argumentsJson}}`
- `step.runCommand.{commandLine, proposedCommandLine, cwd, exitCode, combinedOutput.full, blocking, waitMsBeforeAsync}`
- `step.completedInteractions[].{request, response}` (response echoes the delivered answer)

Status enum observed live: `CORTEX_STEP_STATUS_{DONE, WAITING, ERROR}` (and transient `PENDING/RUNNING/GENERATING` in `metadata.internalMetadata.statusTransitions`).
Step-type enum observed live (9 distinct): `USER_INPUT, CONVERSATION_HISTORY, PLANNER_RESPONSE, CHECKPOINT, RUN_COMMAND, LIST_DIRECTORY, ASK_QUESTION, VIEW_FILE, CODE_ACTION` (VIEW_FILE / CODE_ACTION observed in the trajectory but not all saved as fixtures — the mapper only needs the type/status discriminator + the per-type payload key, which follows the same `camelCase(type)` convention, e.g. `viewFile`, `codeAction`).

### 1.1 ERROR fixture provenance

`run_command_error.json` is the **one synthesized fixture** (all others are verbatim live captures). It was **derived from the live `run_command_waiting.json`** (same conversation `2399249c…`, same real `trajectoryId`/`stepIndex`) by flipping `status` `WAITING`→`ERROR` and appending the `WAITING`→`ERROR` `statusTransition` — i.e. exactly the timeout flip described in design §2.1. The WAITING shape and the timeout-flip behavior are both live-verified; only this exact ERROR *snapshot* is synthesized. The fixture carries an explicit `_fixtureProvenance` string so it can never be mistaken for a verbatim capture (drop/ignore that key when asserting shape).

Why synthesized rather than captured: I made several honest live attempts and none produced an ERROR within a reasonable window:
- left an `ASK_QUESTION` `WAITING` step unanswered for ~3 min → stayed `WAITING` (no timeout);
- left a `RUN_COMMAND` permission `WAITING` step (`echo hello-spike`, index 42) unanswered for >5 min → stayed `WAITING` (no timeout);
- `CancelCascadeSteps {cascadeId}` returned `200 {}` but did **not** flip the `WAITING` step (see §4).

So in agy 1.0.10 the `WAITING`-interaction timeout window is **long (minutes), not seconds** — the §2.1 gotcha is real (the prior memory hit it via slow human delivery) but it is not a quick way to elicit an ERROR step in a spike. Treating ERROR as the labelled-synthesized fallback (per the task brief) was the right call rather than blocking the task.

---

## 2. Step 3 — turn-send verdict

**Verdict: KEEP tmux `send-keys` for user turns. Do NOT use an RPC to send turns.** (Confirms the prior memory + design §2/§7.)

Evidence:
- A turn typed via `tmux send-keys -t agy-spike '<text>' Enter` is recorded as a `CORTEX_STEP_TYPE_USER_INPUT` step with **`metadata.source = CORTEX_STEP_SOURCE_USER_EXPLICIT`** and `userInput.userResponse == "<text>"` (see `user_input.json`). This is exactly what the read path keys on, so send-keys turns are attributed correctly.
- `SendAgentMessage` (the only message-injection RPC on the surface) is documented (memory + design) to record the turn as a `SYSTEM_MESSAGE` ("not actually sent by the user"), which the mapper would then skip/mis-attribute — so it cannot drive user turns. I did **not** re-issue `SendAgentMessage` in this spike (no need to perturb the live session to re-confirm a settled, documented negative; and the mapper already skips USER_INPUT regardless).
- I scanned the live RPC surface for a *proper* user-turn method (a queued-user-input / "send all queued messages" path). The methods exercised/observed on `LanguageServerService` this session were `Heartbeat`, `GetConversationMetadata`, `GetCascadeTrajectorySteps`, `HandleCascadeUserInteraction`, `StreamAgentStateUpdates`. No `SendAllQueuedMessages` / `EnqueueUserInput` / `SubmitUserTurn`-style method was found that records as `USER_INPUT`. **No viable user-turn RPC exists in 1.0.10.**

Implication for the plan: the executor's `run_turn` stays on tmux `send-keys` (design §5/§7 unchanged). Only **interactions** (answers/approvals) and **interrupt** move to RPC.

### 2.1 Important live finding — attended TUI keeps its OWN prompt in parallel with RPC

When agy runs **attended** (auto-exec OFF) and you drive turns by `send-keys`, the **TUI maintains its own permission/question prompt in-process, in parallel with the RPC step state.** Observed live:
- An RPC `HandleCascadeUserInteraction` approval flips the trajectory step to `DONE` and the command runs (verified: `run_command_done.json` has `exitCode:0` + output) — but the **TUI prompt for that same interaction can stay open**, and a subsequent `send-keys` lands in that TUI prompt's filter/amend buffer instead of starting a new turn (observed: a follow-up turn got concatenated into the persist-pattern option text). Pressing `Escape` clears the stale TUI prompt (the TUI then reports "User declined the tool call" for *its* prompt, harmlessly — the RPC-approved command had already run).

Consequences for the production design:
- This is a **non-issue for the real bridge**, which is RPC-driven for interactions and does NOT type interaction answers via the TUI. It is a strong **reason to deliver interactions over RPC, not send-keys**.
- But it means a turn `send-keys`d **while a prior interaction's TUI prompt is still open** can be swallowed. The runner-owned terminal in production should ensure the TUI is at an idle `>` prompt before send-keys'ing a new turn (the read driver already knows the trajectory is idle — no `WAITING`/`RUNNING` step — which is the right gate). Worth a note in Task 11/12.

---

## 3. Step 4 — read-mode verdict

**Verdict: default to `StreamAgentStateUpdates` (server-stream) with `GetCascadeTrajectorySteps` polling as the fallback / reconcile path.** (Matches the memory lean + design §6.)

### 3.1 `GetCascadeTrajectorySteps` (poll) — reliability baseline
- Unary `POST {"cascadeId": conv}` → `200 {"steps":[...]}`. Rock-solid every call this session (dozens of calls, 0 failures). Returns the **complete** step list each time (full snapshot), with explicit per-step `status` — trivial to dedup by `stepIndex`/identity. Typical round-trip a few ms on loopback.
- This is the **simplest correct** read path and the natural reconcile-on-reconnect mechanism. The whole point of the RPC rework (design §3) is that these structured snapshots remove the JSONL cursor/gap logic and fix the double-render.

### 3.2 `StreamAgentStateUpdates` (server-stream) — latency win, framing caveat
- **Request MUST be connect-enveloped.** This is a correction to the memory note: sending a bare JSON body `{"conversationId": conv}` to `StreamAgentStateUpdates` returns a single connect error frame:
  `{"error":{"code":"invalid_argument","message":"... protocol error: promised 576941934 bytes in enveloped message, got 53 bytes ..."}}`
  — the server reads the first 5 bytes of the JSON as the connect envelope header. The body must be framed as `[flag:1=0x00][len:BE-uint32][json-bytes]` (same 5-byte envelope as the response frames). Content-Type `application/connect+json`.
- With the **enveloped** request: `200`, the stream **stays open and long-polls**. First frame carrying steps arrived **~0.13 s after a turn was sent** (measured: `first_steps_frame_at = 0.132 s`); the stream then emits a burst of incremental `update` frames as steps progress (`update.mainTrajectoryUpdate.stepsUpdate.steps[]`), each `flag=0`, then blocks (long-poll) when the trajectory goes idle. A trailing `flag=2` frame carries the connect end-of-stream / error envelope.
- **Reliability caveat:** because the stream blocks when idle, a naive reader must use a read timeout / heartbeat and reconnect, and must **reconcile via a `GetCascadeTrajectorySteps` snapshot on (re)connect** to avoid missing a transition that happened during a gap. The connect framing (envelope on both request and response) is fiddly to get exactly right (cost me one iteration), so the client wrapper must own it and be unit-tested against the captured frames.

### 3.3 Recommendation
- **Default: stream** for low-latency detection of `WAITING` interactions and step progress (~130 ms vs a poll interval), **with poll as the fallback**: (a) reconcile snapshot on every (re)connect, (b) fall back to pure polling if the stream errors/regresses. This matches design §6 ("polls `GetCascadeTrajectorySteps` *or* consumes `StreamAgentStateUpdates`").
- **Acceptable de-scope:** if the connect server-stream framing proves too costly to harden in the implementation tasks, **ship poll-first** (a tight `GetCascadeTrajectorySteps` loop, e.g. 250–500 ms while a turn is active) and add the stream as a follow-up. Polling alone is fully correct (full snapshots + explicit status); the only thing lost is sub-second push latency. The interaction bridge's tight detect→deliver loop (design §2.1) already re-reads the freshest `WAITING` step at delivery time, so poll-first does not compromise interaction correctness.

---

## 4. Other live confirmations (for Tasks 2/3/5/8/10)

- **Approval round-trip (Task 3/8):** `HandleCascadeUserInteraction {cascadeId, interaction:{trajectoryId, stepIndex, permission:{allow:true}}}` → `200 {}`; the `RUN_COMMAND` step flipped `WAITING`→`DONE` with `exitCode:0` and real `combinedOutput.full`. `trajectoryId`+`stepIndex` come from the WAITING step's `metadata.sourceTrajectoryStepInfo`. (Exactly the memory shape; `permission.allow`, no `approvalId`.)
- **Answer round-trip (Task 3/8):** `HandleCascadeUserInteraction {... interaction:{trajectoryId, stepIndex, askQuestion:{responses:[{question:"<verbatim>", selectedOptionIds:["4"]}]}}}` → `200 {}`; the `ASK_QUESTION` step flipped to `DONE` and the cascade proceeded autonomously. `selectedOptionIds` uses the option `id` (`"1".."N"`), not the text.
- **Tool cwd:** agy executes `run_command` in its own scratch dir (`combinedOutput.full` for `pwd` = `/Users/bryanli/.gemini/antigravity-cli/scratch`), NOT the agy launch CWD. Benign, but worth knowing for any cwd-sensitive parity check.
- **`GetConversationMetadata` ownership probe** still works as the discovery module expects (`metadata.rootConversationId` echo) — port discovery via `omnigent/antigravity_native_rpc.py` worked first try.
- **`CancelCascadeSteps` (Task 10) — accepts `{cascadeId}` but does NOT cancel a WAITING-for-interaction step.** `POST CancelCascadeSteps {"cascadeId": conv}` → `200 {}` (so, contrary to the old `antigravity_native_rpc.interrupt_turn` worry, the *conversation/cascade id alone is accepted* as the request key — no internal invocation id was needed for a `200`). **However** the live `RUN_COMMAND` `WAITING` step did **not** change status after the call (still `WAITING`, no new `statusTransition`). So for Task 10: `CancelCascadeSteps {cascadeId}` is wired-up-able with just the conversation id, but its effect on a step that is `WAITING` on a human interaction is a **no-op** here — it likely targets in-flight `RUNNING`/generating steps, not interaction-pending ones. **Task 10 must verify cancel against a RUNNING step** (e.g. cancel mid-generation, or mid-long-command) to confirm it actually interrupts, and should pair cancel-of-an-interaction with delivering a **deny** (`permission.allow:false` / `askQuestion` skip) to actually unblock a `WAITING` step. Whether `ForceStopCascadeTree` behaves differently was not tested.

---

## 5. Concerns / follow-ups

- **ERROR fixture is synthesized** (the only one) — see §1.1. The `WAITING` timeout window in 1.0.10 is minutes-long, so a real ERROR snapshot wasn't elicitable in the spike window. If Tasks 4/5 want a verbatim ERROR step, capture one opportunistically during the Task 13 live run (let an interaction sit, or hit a real tool error) and replace the fixture.
- **`CancelCascadeSteps` is a no-op on WAITING-for-interaction steps** (§4) — Task 10 must validate the real interrupt against a `RUNNING` step, and unblock `WAITING` steps with a deny rather than a cancel. Don't assume `200 {}` == "interrupted".
- **Connect stream framing** (request envelope, §3.2) is a sharp edge — the Task 2/6 client wrapper must own request+response enveloping and be unit-tested against captured frames; do not hand it to callers. If hardening it slips, ship poll-first (§3.3) — fully correct, only loses sub-second latency.
- **Attended TUI vs RPC interaction** (§2.1): production runner should gate `send-keys` turns on an idle trajectory (no `WAITING`/`RUNNING` step); surface in Task 11/12.
- Step payload key follows `camelCase(type)` (e.g. `RUN_COMMAND`→`runCommand`, `VIEW_FILE`→`viewFile`); the mapper can rely on this convention but should default-skip unknown types rather than assume a payload key exists.
