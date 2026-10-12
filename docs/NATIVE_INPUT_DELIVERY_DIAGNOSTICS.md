# Tracing a native web message

`native_prompt_not_recorded` is saved when a later native transcript message
matches a newer queued web input. Its timestamp is when Omnigent discovered
the older missing message. Use the identifiers below to find the original
delivery attempt; a nearby warning may belong to the later message.

The diagnostics contain identifiers, timing, and outcomes. They do not add
prompt text, attachment names, or terminal captures to logs.
The event helper rejects unrecognized fields, malformed identifiers, and
unbounded or whitespace-containing code values. Callers must supply identifiers
and codes rather than put content in an allowed field.

## Identifiers

| Field | Meaning |
| --- | --- |
| `input_stable_id` | The web client's ID for a logical submission, when provided. Connects client retries. |
| `pending_id` | The server's queued entry. Also appears as `cleared_pending_id` on `session.input.consumed`. |
| `delivery_attempt_id` | The server-generated ID for one queued submission attempt. A retry while still pending reuses it without forwarding again. |
| `input_enqueued_at_ms` | Server enqueue time, in Unix milliseconds. Preserved if persistence fails and the pending entry is restored. |
| `response_id` | The Omnigent executor response, or the saved conversation item's response at persistence. These can differ; use the input identifiers to connect the records. |
| `item_id` / `error_item_id` | Durable conversation items. Join the saved error's SSE `item_id` to the settlement's `error_item_id`. |
| `matched_item_id` / `matched_response_id` | The later native record that matched a newer input and caused earlier entries to settle. These belong to that later record. |
| `delivery_id` | Claude's existing local delivery trace ID. Its attempts and timing now carry the input identifiers too. |
| `thread_id` | The Codex thread named by the RPC request. |
| `native_turn_id` | On attempt, error, and cancellation records, the requested steer target. On a successful RPC result, the turn returned by Codex. An unsuccessful record does not establish that Codex accepted or recognized the target. |
| `native_rpc_attempt_id` | Pairs one Codex RPC attempt with its accepted, failed, or cancelled result, including attempts recovered by retrying. |
| `requested_native_turn_id` | The target of that specific steer; absent for a new turn start. |
| `initial_native_turn_id` | Bridge state before a complete injection, which can differ from the target of a recovered RPC attempt. |

Join within the same workspace and Omnigent session. Missing IDs mean that a
producer did not supply them; do not infer ownership from timestamps alone.

## Events

| Event | What it records |
| --- | --- |
| `native_input_enqueued` | The server queued the input. |
| `native_input_retry_deduplicated` | A client retry reused an existing pending entry. No second delivery was requested. |
| `native_input_forward_started` | The server began forwarding the input to the runner. |
| `turn_dispatched` / `native_input_forward_finished` | The forwarding result. `forward_accepted` means the runner accepted the request, not that a native transcript contains it. |
| `native_input_execution_started` / `native_input_execution_finished` | The executor call and its bounded result, including cancellation or an explicitly reported undelivered input. `executor_returned` alone does not prove a transcript record exists. `executor_stream_ended` means the executor exited without reporting a final result. |
| `native_input_steering_started` / `native_input_steering_finished` | A direct in-band harness injection carrying its own input identity. `executor_accepted` means the executor accepted the injection; `executor_refused` means it returned false. `cancelled` means the injection was interrupted, and `error` means it raised an exception. Acceptance does not prove transcript persistence. |
| `claude_native_delivery_finished` | Claude's existing stages, verification, retries, and local delivery ID, now joined to the original input. `draft_cleared` means the submitted draft disappeared; it does not prove native transcript persistence. Other delivery outcomes remain `unknown`. |
| `codex_native_delivery_attempt` / `codex_native_delivery_finished` | One pair per RPC, with `turn_start` or `turn_steer` and an `rpc_accepted`, `rpc_accepted_missing_turn_id`, `rpc_error`, or `cancelled` outcome. The missing-ID outcome means the RPC returned without a nonempty turn ID. A recovered stale steer generates separate pairs for the rejected and retried requests. No new delivery events are emitted without an input identity. |
| `codex_turn_injection_failed` | The input identity, RPC error code when available, and native thread context for a failed injection. An RPC failure can be ambiguous about acceptance. |
| `native_input_settled` | The server's decision after durable persistence. See outcomes below. |
| `native_input_invalid_delivery_stage` | A caller supplied an unsupported server stage. The existing stage is preserved; the caller's invalid value is not logged. |

The older top-level `turn_id` on `codex_turn_injection_failed` is the initial
bridge snapshot, also named `initial_native_turn_id`. Use the individual RPC
records to identify the turn that accepted or rejected a retry.

The native web runner currently buffers messages sent during an active turn
for a continuation turn. Those messages produce `native_input_execution_*`
events. The steering events above apply to direct in-band harness injections
that carry input identity.

Commands such as Codex `/side` that bypass the pending-input queue do not
emit these input-delivery events.

`native_input_settled.outcome` distinguishes:

- `native_transcript_matched`: the pending input matched the mirrored text.
- `native_transcript_fifo_attributed`: no text match was found; the existing
  FIFO fallback attributed the mirror to the oldest input. This is uncertain.
- `skipped_without_native_record`: an older unmatched input was saved with a
  missing-message error. The row includes the saved message and error IDs,
  the later `matched_item_id` and `matched_pending_id`, and `match_method`.
- `prior_fifo_match_uncertain`: an earlier FIFO attribution prevents treating
  this older input as definitely missing.
- `user_interrupted`: the person pressed Stop while this input was queued.
  A later transcript match clears it without saving a missing-message error.
- `reported_undelivered`: the runner explicitly reported that this particular
  input had not reached the harness.

`match_method` is `normalized_text`, `attachment_normalized_text`,
`fifo_fallback`, or `input_stable_id`. `last_delivery_stage` records the last
stage the **server** had observed when it drained the entry: `server_queued`,
`forward_requested`, `forward_accepted`, or `unknown`. It is not a substitute
for the runner's delivery trace. A transcript can arrive before the forwarding
HTTP response, so a settled input can still say `forward_requested`.

SSE logging retains `item.response_id` and the `item_id` and
`cleared_pending_id` nested in `session.input.consumed.data`. That consumed
event clears a pending bubble; it is not proof of delivery. Join it to
`native_input_settled` to distinguish a native match from a skipped message.
`error_item_persisted` also includes its durable item and response IDs.
A runner's `response.failed` event can carry top-level `input_stable_id`.
SSE logging retains this validated ID to connect the failure to its original
web submission, even before a transcript item exists.

## Verification

Run the deterministic persistence check from the repository root:

```sh
uv run --no-sync pytest -q tests/server/routes/test_native_input_diagnostics.py
```

It queues two messages, mirrors only the second, and checks that the first
message's saved error is joined to its own input and to the later match. It
also covers append failure, forwarder replay, and FIFO uncertainty.

For a manual check, run `omnidev`, open a Claude Native or Codex Native
session in the displayed UI, and send a short message. In structured server
and harness logs, search for that session's `native_input_enqueued` event.
Follow its `pending_id` through dispatch, the harness delivery event, and
`native_input_settled`. The input IDs should agree while the saved item ID
can differ. No prompt text should appear in these new diagnostic fields.

The queue remains in memory. A server restart can lose its correlation state.
Native transcript matching remains text-based; identical prompts and FIFO
fallback retain their existing uncertainty. Claude prompt-submit hooks have
no reliable input ID echo, so this instrumentation does not assign them an
input ID or claim to establish every missing message's root cause.
