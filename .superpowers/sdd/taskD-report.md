# Task T-D — Reader streaming mode (output_text_delta + poll fallback)

**Status:** DONE_WITH_CONCERNS (one reconciliation judgment call below; reasoning-stream skipped by contract). All gates green; 19 reader tests + 87 reader/steps tests pass.

## What was built

Stream-primary read driver in `omnigent/antigravity_native_reader.py`:

- `supervise_reader` now discovers `(cascade_id, port)` once, builds one shared
  `_ReaderState` (allocator + `seen` + `interacted` + per-step `prefixes` +
  `turn_active`), then **tries `_stream_loop`**; on `httpx.HTTPError` /
  `AntigravityRpcError` it **falls back to `_poll_loop`** (the prior Task-6
  committed-only loop, refactored out verbatim). A stream error never kills the
  reader — graceful degradation to Phase-1 behaviour. The shared `_ReaderState`
  makes the fallback idempotent against whatever the stream already delivered.
- `_stream_loop` consumes `stream_agent_state_updates(port, cascade_id)`,
  extracts `update.mainTrajectoryUpdate.stepsUpdate.steps[]` per frame
  (`_frame_steps`), and routes each step through `_process_stream_step`.

## The delta / prefix-diff design (`_process_stream_step` → `_emit_partial_delta`)

- A PLANNER_RESPONSE step with `status == CORTEX_STEP_STATUS_GENERATING` is
  intercepted (`_is_generating_planner`) BEFORE the committed path. Its growing
  `plannerResponse.modifiedResponse` (`_partial_planner_text`) is prefix-diffed
  against `state.prefixes[step_index]`: the **new suffix** is emitted as one
  `external_output_text_delta` (`final=False`, stable per-step `message_id`), and
  the tracker advances to the full cumulative text. Guard `text.startswith(forwarded)
  and len(text) > len(forwarded)` means a no-growth re-send (cumulative snapshot)
  or a non-extending rewrite emits **nothing** but still re-anchors the tracker —
  deltas never overlap or duplicate, and they concatenate exactly to the full text.
- A GENERATING planner is **never** added to `seen`, so its eventual DONE frame
  still commits.

## The reconciliation contract (delta-first, single render)

- Per the contract: incremental `external_output_text_delta` events during
  GENERATING (stable `message_id = antigravity:<conv>:<stepIndex>:planner`,
  `index:0`, growing suffixes), then the COMMITTED `message` via
  `map_step_to_events` when the step reaches DONE. Streaming guarantees
  deltas-precede-committed naturally (GENERATING frames arrive before the DONE
  frame). The stable id lets the SPA coalesce deltas into one live block and
  retire it on the committed item → single render.
- Tests assert exactly this: deltas concatenate to the full text, share one
  `message_id`, are all `final=False`; the committed `message` is emitted exactly
  ONCE and AFTER all deltas (index assertion: `max(delta_idx) < committed_idx`).

## Dedup (snapshot replay + non-contiguous tool steps)

- Committed items dedup by `(trajectory_id, step_index)` in `state.seen` so the
  on-connect snapshot replay and cumulative re-sends post nothing.
- **Settled-only dedup (`_is_settled`):** a step's identity is recorded in `seen`
  ONLY once it is terminal (`DONE`/`ERROR`) or a USER_INPUT. This fixes a latent
  drop: a tool-result step is observed through PENDING/RUNNING/WAITING before DONE
  (verified in `run_command_done.json` statusTransitions), and the mapper emits its
  output only at DONE. Recording it on a pre-DONE sighting (mapper → `[]`) would
  dedup the later DONE and silently DROP the `function_call_output`. The stream
  surfaces every intermediate status, so this is far likelier on the stream path
  than the coarse 0.25s poll. A not-yet-settled step is re-emitted (a safe no-op:
  maps to `[]`, fires no status edge) until it settles. Regression test:
  `test_stream_tool_result_running_then_done_emits_output`.
- On a planner commit, `_process_stream_step` clears `state.prefixes[step_index]`
  so an agy timeout-retry reusing the slot starts a fresh delta stream rather than
  diffing against stale text.

## Where the delta builder lives

Relocated out of the soon-retired forwarder into the mapper module
(`omnigent/antigravity_native_steps.py`), its Task-12 home alongside `OutboundEvent`
and `map_step_to_events`:

- `output_text_delta_event(*, conversation_id, step_idx, delta, final)` — builds
  `external_output_text_delta` with `data={"delta", "message_id", "index":0, "final"}`.
  Unlike the forwarder's one-shot (`final=True`, whole DONE text), this carries a
  **suffix** with `final` configurable; the streaming reader passes `final=False`
  and relies on the committed `message` (not a `final` delta) to close the block.
- `planner_message_id(conversation_id, step_idx)` — the stable
  `antigravity:<conv>:<stepIndex>:planner` id, shared by all of a step's deltas.

The new reader imports these from `antigravity_native_steps`, NOT from the
forwarder — no new dependency on the soon-deleted module. The forwarder's own
`_output_text_delta_event` is left untouched (it dies with the forwarder in Task 12).

## Reasoning-delta handling (CONCERN — judgment call)

**Decision: reasoning-streaming is SKIPPED.** Investigated the SPA/server contract:
the only external delta ingest is `external_output_text_delta`
(`omnigent/server/routes/sessions.py:324`). There is **no** `external_reasoning_text`
POST event type (the full external-event list at sessions.py:312-477 has none).
The SSE `response.reasoning_text.delta` exists but is fed by the in-process
workflow / codex executor path, not by any external POST a terminal-backed harness
can emit. Folding `plannerResponse.thinking` into `external_output_text_delta` would
corrupt the assistant message (thinking is not the answer; the committed `message`
carries only `response`/`modifiedResponse`). Per the task ("if none, you may emit
thinking via the same delta mechanism or skip reasoning-streaming and note it — don't
invent a new SPA contract"), I skipped it rather than invent a contract. If
reasoning-streaming parity is wanted, it needs a new external reasoning-delta POST
endpoint + SSE plumbing — out of scope for T-D; flag for T-EFG / Task 13.

## Other concerns

- **`stop`-budget arithmetic in tests.** The reader now runs two loops (stream
  then poll-fallback). The three pre-existing poll tests share one finite `stop`
  counter across both, so I bumped their `iterations` (2→3, and the placeholder
  test to 4) and documented why. Production (`stop=None`) is unaffected: stream
  runs until error, then poll runs forever. Mechanical, but worth a reviewer's eye.
- **Status edges on the stream path.** RUNNING/IDLE edges still come from
  `_emit_step` (USER_INPUT opens, assistant-text-close closes), now driven by
  stream frames. `test_stream_status_running_then_idle` covers it. The IDLE edge
  still keys on a DONE assistant-text planner with no tool calls — unchanged
  heuristic from Task 6.
- **Single-render is live-verified by Task 13**, not here. These are unit tests
  with scripted frames; they prove the delta-first ordering + stable id + dedup
  the SPA relies on, but the actual SPA retire-on-committed behaviour is a Task-13
  e2e assertion.

## TDD evidence

1. Wrote failing stream tests (stream attr not present on reader → `AttributeError`).
2. Ran → FAIL (3 poll tests + all new stream tests failed/errored as expected).
3. Implemented: relocated delta builder to steps; stream-primary `supervise_reader`
   with `_stream_loop` / `_poll_loop` / `_process_stream_step` / `_emit_partial_delta`
   / `_is_settled`.
4. Ran → PASS (19 reader, 87 reader+steps). Forwarder+rpc suites: 149 pass (no regress).

## Pre-commit gates (scoped)

- `ruff check --fix` → All checks passed.
- `ruff format` → unchanged (after the run that reformatted 2).
- `mypy --strict` (3 files) → Success: no issues found.
- `pytest tests/test_antigravity_native_reader.py tests/test_antigravity_native_steps.py -v` → 87 passed.
- `grep -rn "type: ignore\|# noqa" <3 files>` → empty (exit 1, no matches).

## Files changed

- `omnigent/antigravity_native_reader.py` — stream-primary loop, `_ReaderState`,
  `_stream_loop`, `_poll_loop` (refactor of the old inline loop),
  `_frame_steps`, `_process_committed_step`, `_process_stream_step`,
  `_is_generating_planner`, `_partial_planner_text`, `_emit_partial_delta`,
  `_is_settled`; new imports (`AntigravityRpcError`, `stream_agent_state_updates`,
  `output_text_delta_event`, `dataclass`/`field`); `_STATUS_GENERATING` /
  `_TERMINAL_STATUSES` constants.
- `omnigent/antigravity_native_steps.py` — `output_text_delta_event` +
  `planner_message_id` (relocated delta builder; suffix + configurable `final`).
- `tests/test_antigravity_native_reader.py` — stream scaffolding (`_frame`,
  `_generating_planner`, `_done_planner`, `_running_run_command`, `_FrameScript`,
  `_RaisingStream`, `_run_stream`); `_PostSink.deltas()` / `.event_types()`; 9 new
  stream tests; 3 poll tests adjusted for the stream-attempt `stop` tick.
