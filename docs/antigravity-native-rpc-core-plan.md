# Antigravity-native RPC Core Rework — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework the antigravity-native harness runtime onto agy's connect-RPC surface — structured trajectory-step reads, interaction bridging (questions + approvals), and real interrupt — replacing the JSONL transcript-tail + send-keys-interaction core.

**Architecture:** agy keeps running in a runner-owned tmux terminal (terminal-first UX). A new RPC client (extending `antigravity_native_rpc.py`) drives: read via `GetCascadeTrajectorySteps`/`StreamAgentStateUpdates`, interactions via `HandleCascadeUserInteraction` (surfaced as omnigent elicitations), and interrupt via `CancelCascadeSteps`. A pure step→item mapper replaces `step_to_events`. Turn-send stays on tmux send-keys (pending a confirmed user-turn RPC). The transcript forwarder + durable cursor are retired.

**Tech Stack:** Python 3.13+ (uv), httpx (connect-RPC over HTTP/2, JSON), pytest, existing omnigent server elicitation registry + SSE, agy 1.0.10.

**Design spec:** `docs/antigravity-native-rpc-core-design.md`. **Verified wire shapes:** memory `agy-rpc-interaction-bridge.md`.

## Global Constraints

- Python deps via `uv` only (never pip); JS/TS via `bun`. Latest stable deps.
- Pre-commit gate (must pass): `uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest`. Never disable a lint/type rule — fix the root cause.
- agy pinned: `AGY_EXPECTED_VERSION=1.0.10` (Docker build fails on mismatch). All RPC shapes are version-sensitive.
- connect-RPC: JSON (`Content-Type: application/json`), `verify=False`, every URL passes `_assert_loopback_url`. Reuse `antigravity_native_rpc.py` discovery (`discover_language_server_port` / `_candidate_agy_rpc_ports` / `_conversation_matches`).
- Identity: `cascadeId == conversationId == brain-dir UUID` (no separate id lookup).
- Interaction envelope: `HandleCascadeUserInteraction {cascadeId, interaction:{trajectoryId, stepIndex, <variant>}}` — `trajectoryId`+`stepIndex` MUST be inside `interaction`. Question variant `askQuestion.responses:[{question, selectedOptionIds:[id]}]`; approval variant `permission:{allow:bool}`.
- Timeout gotcha: `WAITING` interactions expire (→ `ERROR`, agy auto-retries at a higher `stepIndex`). Always re-read the freshest `WAITING` step at delivery; `500 "input not registered"` ⇒ missing `trajectoryId` **or** a stale step — check `status` first.
- Frequent commits; TDD (red→green) per task; commit messages end with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

## File Structure

- `omnigent/antigravity_native_rpc.py` (modify) — add interaction/step/cancel RPC methods alongside the existing discovery helpers.
- `omnigent/antigravity_native_steps.py` (create) — pure step→item mapper (replaces the mapping half of `step_to_events`) + the `WAITING`-interaction extractor.
- `omnigent/antigravity_native_reader.py` (create) — read driver: poll/stream trajectory steps → post items; owns dedup + the interaction-bridge loop.
- `omnigent/antigravity_native_interactions.py` (create) — interaction bridge: detect→elicitation→deliver, with the timeout/re-read loop.
- `omnigent/server/routes/_antigravity_elicitation.py` (create) — parse an agy `WAITING` interaction → `ElicitationRequestParams`, and map an `ElicitationResult` back to an `interaction` payload (mirrors `_codex_elicitation.py`).
- `omnigent/inner/antigravity_native_executor.py` (modify) — `interrupt_session` → `CancelCascadeSteps`; (turns unchanged pending Task 1).
- `omnigent/runner/app.py` (modify) — auto-create the RPC reader instead of (or alongside, during cutover) the transcript forwarder.
- `omnigent/antigravity_native_forwarder.py` (delete at cutover, Task 12).
- Tests mirror each module under `tests/`.

---

### Task 1 (Spike): Confirm turn-send + read-mode, enumerate step types

**Files:**
- Create: `docs/claude/antigravity-rpc-spike-notes.md` (findings)
- Create: `tests/fixtures/antigravity/steps/*.json` (recorded trajectory-step fixtures)

**Interfaces:**
- Produces: the recorded step fixtures (one per agy step type) consumed by Tasks 4–5; decisions for Tasks 2 (turn-send), 6 (poll-vs-stream).

- [ ] **Step 1:** Stand up a live agy on the running omnigent (`:6767`, HOST_ID `host_1b3116a52bf74d668d3179652c54cdc7`, `HOME=/Users/bryanli`) per memory `agy-rpc-interaction-bridge.md`; discover the RPC port via `discover_language_server_port`.
- [ ] **Step 2:** Drive turns/tools/questions to exercise each step type; save each distinct `GetCascadeTrajectorySteps` step (USER_INPUT, PLANNER_RESPONSE w/ text, PLANNER_RESPONSE w/ tool_calls incl. `ask_question`, RUN_COMMAND WAITING + DONE, other MODEL/result steps, status edges) as a fixture JSON.
- [ ] **Step 3:** Determine turn-send: check for a user-turn RPC (e.g. a queued-user-input / `SendAllQueuedMessages` path) that records as `USER_INPUT` (not `SYSTEM_MESSAGE`). Record verdict: RPC turn-send viable, or keep tmux send-keys.
- [ ] **Step 4:** Determine read-mode: exercise `StreamAgentStateUpdates {conversationId}` (connect server-stream framing) vs `GetCascadeTrajectorySteps` polling; record latency + reliability; pick the default (likely stream + poll fallback).
- [ ] **Step 5:** Write findings + decisions to the notes doc; commit fixtures + notes. **Acceptance:** every step type has a fixture; turn-send + read-mode decisions are recorded with evidence.

---

### Task 2: RPC client — trajectory steps + cancel (unary)

**Files:**
- Modify: `omnigent/antigravity_native_rpc.py`
- Test: `tests/test_antigravity_native_rpc.py`

**Interfaces:**
- Produces: `get_trajectory_steps(port:int, cascade_id:str) -> list[dict]`; `cancel_cascade_steps(port:int, cascade_id:str) -> bool`. Both reuse `_rpc_url`/`_sync_client`/`_assert_loopback_url`.

- [ ] **Step 1: Write failing tests** (MockTransport, mirroring `test_conversation_matches_*`):

```python
def test_get_trajectory_steps_posts_cascade_id_and_returns_steps(monkeypatch):
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["body"] = req.content
        return httpx.Response(200, json={"steps": [{"stepIndex": 0, "status": "CORTEX_STEP_STATUS_DONE"}]})
    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(handler))
    steps = rpc.get_trajectory_steps(52548, "conv-uuid")
    assert seen["url"].endswith("/LanguageServerService/GetCascadeTrajectorySteps")
    assert json.loads(seen["body"]) == {"cascadeId": "conv-uuid"}
    assert steps[0]["stepIndex"] == 0

def test_cancel_cascade_steps_true_on_200(monkeypatch):
    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert rpc.cancel_cascade_steps(52548, "conv-uuid") is True
```

- [ ] **Step 2: Run, verify FAIL** — `uv run pytest tests/test_antigravity_native_rpc.py -k "trajectory_steps or cancel_cascade" -v` → fail (undefined).
- [ ] **Step 3: Implement** `get_trajectory_steps` (POST `{"cascadeId": cascade_id}` to `GetCascadeTrajectorySteps`, parse `.get("steps", [])`) and `cancel_cascade_steps` (POST `{"cascadeId": cascade_id}` to `CancelCascadeSteps`, return `resp.status_code < 400`), both via `_sync_client` + `_assert_loopback_url`, mirroring `_conversation_matches`.
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** (`feat(antigravity-native): RPC client — trajectory steps + cancel`).

---

### Task 3: RPC client — `handle_user_interaction` (unary)

**Files:** Modify `omnigent/antigravity_native_rpc.py`; Test `tests/test_antigravity_native_rpc.py`

**Interfaces:**
- Produces: `handle_user_interaction(port:int, cascade_id:str, *, trajectory_id:str, step_index:int, payload:dict) -> None` (raises `AntigravityRpcError` on non-200, carrying the body so callers can detect the overloaded `"input not registered"`). `payload` is the variant dict, e.g. `{"askQuestion": {...}}` or `{"permission": {"allow": True}}`.

- [ ] **Step 1: Write failing tests:**

```python
def test_handle_user_interaction_nests_traj_and_step_inside_interaction(monkeypatch):
    seen = {}
    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(
        lambda r: seen.setdefault("body", r.content) and None or httpx.Response(200, json={})))
    rpc.handle_user_interaction(52548, "c", trajectory_id="t", step_index=14,
                                payload={"permission": {"allow": True}})
    assert json.loads(seen["body"]) == {
        "cascadeId": "c",
        "interaction": {"trajectoryId": "t", "stepIndex": 14, "permission": {"allow": True}}}

def test_handle_user_interaction_raises_on_500(monkeypatch):
    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(500, json={"message": "input not registered for step 14"})))
    with pytest.raises(rpc.AntigravityRpcError) as ei:
        rpc.handle_user_interaction(52548, "c", trajectory_id="t", step_index=14, payload={"permission": {"allow": True}})
    assert "input not registered" in str(ei.value)
```

- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** `AntigravityRpcError(Exception)` (if absent) and `handle_user_interaction`: build `{"cascadeId": cascade_id, "interaction": {"trajectoryId": trajectory_id, "stepIndex": step_index, **payload}}`, POST to `HandleCascadeUserInteraction`; on `>= 400` raise `AntigravityRpcError(body_text)`.
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** (`feat(antigravity-native): RPC client — handle_user_interaction`).

---

### Task 4: Step→item mapper — assistant text, tool calls, results, status

**Files:** Create `omnigent/antigravity_native_steps.py`; Test `tests/test_antigravity_native_steps.py`

**Interfaces:**
- Consumes: Task 1 step fixtures.
- Produces: `map_step_to_events(step:dict, *, conversation_id:str, allocator:_ToolCallIdAllocator) -> list[OutboundEvent]` (reuses `OutboundEvent` + `_ToolCallIdAllocator` from the forwarder module, to be relocated here at cutover). Skips `USER_INPUT` steps. Emits exactly one `message` per assistant text step (NO delta).

- [ ] **Step 1: Write failing tests** against the fixtures: assert PLANNER_RESPONSE text → one `external_conversation_item` `message` (role assistant, single `output_text`, **no** `output_text_delta`); USER_INPUT → `[]` (skipped — fixes user-dup); RUN_COMMAND DONE → `function_call` + `function_call_output` with `runCommand.combinedOutput.full`; status edges (RUNNING/IDLE) emitted per the verified `status` field. (Use the exact assertions from the recorded fixtures.)
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** `map_step_to_events` per the fixtures (port the non-delta logic from `step_to_events`; drop the delta emission and the USER_INPUT mirror; key tool-call ids via the allocator).
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** (`feat(antigravity-native): pure step→item mapper (no delta, skip USER_INPUT)`).

---

### Task 5: Mapper — `WAITING` interaction extractor

**Files:** Modify `omnigent/antigravity_native_steps.py`; Test `tests/test_antigravity_native_steps.py`

**Interfaces:**
- Produces: `pending_interaction(step:dict) -> PendingInteraction | None` where `PendingInteraction` = `{kind: "ask_question"|"permission", trajectory_id, step_index, spec: dict}` (spec = the `askQuestion`/`permission` block + `runCommand`/question text). Returns `None` unless `status == CORTEX_STEP_STATUS_WAITING`.

- [ ] **Step 1: Write failing tests** from the WAITING fixtures: ask_question WAITING → `kind=="ask_question"`, `spec.questions[].options[].{id,text}`, `trajectory_id`/`step_index` from `metadata.sourceTrajectoryStepInfo`; run_command WAITING → `kind=="permission"`, `spec.resource.{action,target}`; DONE step → `None`.
- [ ] **Step 2–4:** Run FAIL → implement `pending_interaction` (read `requestedInteraction`, branch on `askQuestion`/`permission`, pull ids from `metadata.sourceTrajectoryStepInfo`) → run PASS.
- [ ] **Step 5: Commit** (`feat(antigravity-native): WAITING-interaction extractor`).

---

### Task 6: Read driver — poll/stream steps → post items

**Files:** Create `omnigent/antigravity_native_reader.py`; Test `tests/test_antigravity_native_reader.py`

**Interfaces:**
- Consumes: `get_trajectory_steps` (Task 2), `map_step_to_events` (Task 4), `pending_interaction` (Task 5), `_native_post_delivery.post_session_event_with_retry`.
- Produces: `async supervise_reader(bridge_dir, session_id, *, client, poll_interval_s=...) -> None` — discovers the port, loops reading steps, maps + posts new ones (dedup by `stepIndex` identity), and hands `WAITING` steps to the interaction bridge (Task 8). Read-mode (poll vs `StreamAgentStateUpdates`) per Task 1 Step 4.

- [ ] **Step 1: Write failing tests** with a fake step source (monkeypatch `get_trajectory_steps` to return a scripted sequence) + a captured post sink: assert each new step posts exactly once (re-reads dedup), USER_INPUT posts nothing, and a WAITING step invokes the bridge callback once.
- [ ] **Step 2–4:** Run FAIL → implement the loop (dedup set over `(trajectoryId, stepIndex)`; call the bridge for WAITING) → run PASS.
- [ ] **Step 5: Commit** (`feat(antigravity-native): RPC read driver`).

---

### Task 7: Server — agy elicitation adapter

**Files:** Create `omnigent/server/routes/_antigravity_elicitation.py`; Test `tests/server/test_antigravity_elicitation.py`

**Interfaces:**
- Produces: `to_elicitation_params(pending:dict) -> ElicitationRequestParams` (ask_question → form with the options; permission → accept/decline with the command preview); `to_interaction_payload(kind:str, result:ElicitationResult, spec:dict) -> dict` (form/accept → `{"askQuestion": {"responses": [...]}}` or `{"permission": {"allow": True}}`; decline/cancel → `{"permission": {"allow": False}}`). Mirrors `_codex_elicitation.py`.

- [ ] **Step 1: Write failing tests:** ask_question pending → form params with each option; an `ElicitationResult(action="accept", content={"selectedOptionIds":["2"]})` → `{"askQuestion":{"responses":[{"question":..., "selectedOptionIds":["2"]}]}}`. permission pending → accept/decline params; accept → `{"permission":{"allow":True}}`, decline → `{"permission":{"allow":False}}`.
- [ ] **Step 2–4:** Run FAIL → implement both mappers → run PASS.
- [ ] **Step 5: Commit** (`feat(server): antigravity elicitation adapter`).

---

### Task 8: Interaction bridge — detect → elicit → deliver (timeout loop)

**Files:** Create `omnigent/antigravity_native_interactions.py`; Test `tests/test_antigravity_native_interactions.py`

**Interfaces:**
- Consumes: `pending_interaction` (Task 5), `handle_user_interaction` (Task 3), the elicitation adapter (Task 7), the server hook/registry (`_publish_and_wait_for_harness_elicitation` via a new `POST /v1/sessions/{id}/hooks/antigravity-elicitation-request`, mirroring codex).
- Produces: `async bridge_interaction(port, cascade_id, pending, *, client, get_steps) -> None` — publishes the elicitation (deterministic id `(cascade_id, trajectory_id, step_index)`), awaits the resolution (long-poll), then **re-reads the freshest WAITING step** and POSTs `handle_user_interaction`; on the overloaded `"input not registered"` (step timed out → status ERROR), re-reads and re-publishes against the retry step.

- [ ] **Step 1: Write failing tests:** (a) happy path — pending question → elicitation published → resolved("2") → `handle_user_interaction` called with the freshest step's `{trajectoryId, stepIndex}` and `askQuestion.responses[0].selectedOptionIds==["2"]`. (b) timeout path — first delivery raises `AntigravityRpcError("input not registered")` while `get_steps` now shows a NEW WAITING step at a higher index → bridge re-reads and re-delivers to the new step (assert second call uses the new `stepIndex`). (c) permission accept → `{"permission":{"allow":True}}`.
- [ ] **Step 2–4:** Run FAIL → implement the detect→elicit→deliver loop with the re-read-on-stale handling (check step `status` to disambiguate the 500) → run PASS.
- [ ] **Step 5: Commit** (`feat(antigravity-native): interaction bridge with timeout re-read`).

---

### Task 9: Server — antigravity elicitation hook endpoint

**Files:** Modify `omnigent/server/routes/sessions.py`; Test `tests/server/test_app.py`

**Interfaces:**
- Consumes: Task 7 adapter, `_publish_and_wait_for_harness_elicitation`.
- Produces: `POST /v1/sessions/{id}/hooks/antigravity-elicitation-request` — body = the pending-interaction dict; parks + awaits the verdict (deterministic id), returns the `ElicitationResult` (or 200 empty on timeout). Mirrors `POST /hooks/codex-elicitation-request`.

- [ ] **Step 1–4:** TDD: failing route test (post a pending question, resolve via `/elicitations/{eid}/resolve`, assert the hook returns the verdict) → implement the route → pass.
- [ ] **Step 5: Commit** (`feat(server): antigravity elicitation hook endpoint`).

---

### Task 10: Executor — real interrupt via `CancelCascadeSteps`

**Files:** Modify `omnigent/inner/antigravity_native_executor.py`; Test `tests/inner/test_antigravity_native_executor.py`

**Interfaces:**
- Consumes: `cancel_cascade_steps` (Task 2), port discovery, bridge state (cascade id).
- Produces: `interrupt_session` returns `True` after a successful `CancelCascadeSteps`.

- [ ] **Step 1: Write failing test:** `interrupt_session` (monkeypatched `cancel_cascade_steps` → True) returns `True`; on RPC error returns `False`.
- [ ] **Step 2–4:** Run FAIL → implement (discover port from bridge state's conversation id, call `cancel_cascade_steps`) → run PASS.
- [ ] **Step 5: Commit** (`feat(antigravity-native): real interrupt via CancelCascadeSteps`).

---

### Task 11: Runner — auto-create the RPC reader

**Files:** Modify `omnigent/runner/app.py`; Test `tests/runtime/.../test_*`

**Interfaces:**
- Consumes: `supervise_reader` (Task 6) + the interaction bridge (Task 8).
- Produces: the runner's antigravity auto-create starts the RPC reader (+ bridge) instead of the transcript forwarder. Single reader per session (same task-registry guard as the old forwarder).

- [ ] **Step 1: Write failing test:** auto-create starts the reader task (not the forwarder); reattach/cleanup semantics preserved (mirror the existing forwarder-task tests).
- [ ] **Step 2–4:** Run FAIL → swap the forwarder spawn for the reader spawn → run PASS.
- [ ] **Step 5: Commit** (`feat(antigravity-native): runner auto-creates RPC reader`).

---

### Task 12: Cutover — retire the transcript forwarder + durable cursor

**Files:** Delete `omnigent/antigravity_native_forwarder.py`; Modify `omnigent/antigravity_native_bridge.py` (drop `forwarded_steps`/cursor), `omnigent/inner/antigravity_native_executor.py` (drop send-keys-only-for-interactions notes), references; relocate `OutboundEvent`/`_ToolCallIdAllocator` into `antigravity_native_steps.py`; delete `tests/test_antigravity_native_forwarder.py`.

**Interfaces:** None new — removes dead code once the reader path is green.

- [ ] **Step 1:** Grep for `antigravity_native_forwarder` / `forwarded_steps` / `update_forwarded_steps` references; confirm only the reader path remains.
- [ ] **Step 2:** Delete the forwarder module + its tests; remove the cursor fields/methods from the bridge; relocate the shared types.
- [ ] **Step 3:** Run the full gate: `uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest` (targeted antigravity suites + server).
- [ ] **Step 4:** Commit (`refactor(antigravity-native): retire transcript forwarder + durable cursor (RPC reader supersedes)`).

---

### Task 13: Live end-to-end parity verification

**Files:** Create `tests/e2e/antigravity_native_rpc_e2e.py` (or a documented manual script) — gated on a live agy.

- [ ] **Step 1:** Live run on `:6767`: a text turn renders **once** (no double-render); the user prompt appears **once** (no dup) — both live and after refresh.
- [ ] **Step 2:** A question turn surfaces a web elicitation; resolving it answers agy (trajectory proceeds).
- [ ] **Step 3:** A tool turn surfaces an approval elicitation; accepting runs the command (output mirrored); declining blocks it.
- [ ] **Step 4:** Interrupt mid-turn cancels the cascade.
- [ ] **Step 5:** Record results; commit the script/notes (`test(antigravity-native): live RPC parity verification`).

---

## Self-Review

- **Spec coverage:** read path (Tasks 4,6), interactions (Tasks 5,7,8,9), interrupt (Task 10), removal of forwarder/cursor/delta/user-dup (Tasks 4,12), reuse of periphery (unchanged), turn-send decision (Task 1), timeout gotcha (Task 8), poll-vs-stream (Tasks 1,6). Covered.
- **Open questions → tasks:** turn-send RPC = Task 1/2; poll-vs-stream = Task 1/6; elicitation↔timeout = Task 8; #892 packaging = process decision (evolve branch; out of code scope).
- **Type consistency:** `handle_user_interaction(payload=variant dict)` (Task 3) ↔ `to_interaction_payload` returns that variant dict (Task 7) ↔ bridge passes it (Task 8). `pending_interaction` shape (Task 5) ↔ consumed by reader (Task 6) + adapter (Task 7) + bridge (Task 8). Consistent.
- **Placeholders:** Tasks 1, 4–6, 9, 11 reference recorded fixtures/exact assertions to be filled from Task 1's fixtures (a real dependency, not a placeholder); all RPC shapes are concrete from the spec/memory.
