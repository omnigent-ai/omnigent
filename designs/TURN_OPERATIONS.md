# Durable turn operations

Status: proposed foundation for [#4861](https://github.com/omnigent-ai/omnigent/issues/4861)

## Purpose

External orchestrators need a typed, replay-safe way to submit one turn to an
existing Omnigent session. The existing internal `POST /v1/sessions/{id}/events`
surface is intentionally broad and generates a new response identifier for each
request, so it cannot safely be retried after a timeout.

An external operation is a turn submission, not the lifetime of the containing
session. Session-shell creation and turn submission therefore use separate
idempotency domains.

## Durable state machine

```text
accepted -> input_persisted -> dispatched -> succeeded
                              |          \-> failed
                              |          \-> cancelled
                              |          \-> timed_out
                              |
                              \-> dispatch_unknown -> terminal or reconciled dispatched
```

- `accepted`: the canonical request and replay key are durable; no conversation
  input is yet guaranteed.
- `input_persisted`: the journal is durably bound to one conversation item.
- `dispatched`: runner acceptance was observed for this operation identifier.
- `dispatch_unknown`: the server cannot prove whether the runner accepted the
  request. Automated code must query runner status before redispatching.
- terminal states are immutable; an exact repeated report is a no-op and any
  changed report is a conflict.

## Replay boundary

The database enforces uniqueness over:

```text
(workspace_id, conversation_id, principal_hash, idempotency_key_hash)
```

Raw principals and idempotency keys are never persisted. The canonical request
is stored because recovery from `accepted` must not depend on a client resending
the body. The request digest is checked together with the canonical JSON, so an
exact replay returns the same operation while the same key with another body is
a conflict.

## Crash and ambiguity rules

1. Persist the operation before appending conversation input.
2. Bind the operation to exactly one persisted input item before forwarding.
3. Persist the exact runner dispatch envelope with that item. Recovery reloads
   this envelope; it never reconstructs routing, model, file, or item metadata.
4. Propagate the operation identifier through the server-runner boundary.
5. A runner must deduplicate that identifier before this API can automate
   redispatch.
6. A transport timeout after forwarding records `dispatch_unknown`; it is not
   treated as proof that nothing ran.
7. Before each dispatch attempt, the coordinator records the runner's
   `runner_incarnation_id`. An exact retry is permitted only against that same
   incarnation, where the runner operation registry can deduplicate it.
8. A missing operation on the same incarnation can be retried only while the
   durable state is `input_persisted` or `dispatch_unknown`. A runner that loses
   an already acknowledged `dispatched` operation is a protocol failure.
9. A missing operation after the runner incarnation changes is terminal
   ambiguity (`timed_out` with an explicit restart error), never evidence that
   the prior request did not execute and never permission to redispatch.
10. Runner status/reconciliation must resolve ambiguity before retry. A terminal
   observation may resolve `dispatch_unknown` directly.
11. Durable cursor publication and terminal evidence are separate follow-up
   contracts. Live SSE remains an optimization, never the recovery source of
   truth.

This foundation deliberately exposes no public turn endpoint yet. The endpoint
must not be enabled until runner-side deduplication and status lookup exist, or
client retries could duplicate billable work and tool side effects.
