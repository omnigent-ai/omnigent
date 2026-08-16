# Durable turn operations

Status: proposed v1alpha1 contract for
[#4861](https://github.com/omnigent-ai/omnigent/issues/4861)

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
  input is yet guaranteed. The row may already contain a prepared deterministic
  item id and frozen dispatch envelope; it does not claim persistence until the
  item write has completed.
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
2. Prepare one deterministic item id and exact runner dispatch envelope before
   appending. Exact replay checks for that item while holding the conversation's
   existing write lock, so a crash or concurrent retry cannot append it twice
   even when conversation and journal data use separate databases.
3. Advance to `input_persisted` only after the deterministic item exists.
4. Persist the exact runner dispatch envelope with that item. Recovery reloads
   this envelope; it never reconstructs routing, model, file, or item metadata.
5. Propagate the operation identifier through the server-runner boundary.
6. A runner must deduplicate that identifier before this API can automate
   redispatch.
7. A transport timeout after forwarding records `dispatch_unknown`; it is not
   treated as proof that nothing ran.
8. Before each dispatch attempt, the coordinator records the runner's
   `runner_incarnation_id`. An exact retry is permitted only against that same
   incarnation, where the runner operation registry can deduplicate it.
9. A missing operation on the same incarnation can be retried only while the
   durable state is `input_persisted` or `dispatch_unknown`. A runner that loses
   an already acknowledged `dispatched` operation is a protocol failure.
10. A missing operation after the runner incarnation changes is terminal
   ambiguity (`timed_out` with an explicit restart error), never evidence that
   the prior request did not execute and never permission to redispatch.
11. Runner status/reconciliation must resolve ambiguity before retry. A terminal
   observation may resolve `dispatch_unknown` directly.
12. Durable cursor publication and terminal evidence are separate follow-up
   contracts. Live SSE remains an optimization, never the recovery source of
   truth.

## Public v1alpha1 surface

`POST /v1/sessions/{session_id}/turn-operations` requires authenticated edit
access and an `Idempotency-Key` header. Its body is versioned and accepts only a
single user-message `SessionEventInput`; server-owned attribution cannot be
supplied by the client. Exact replay returns the same operation and item, while
key reuse with a changed canonical request returns a conflict.

`GET /v1/sessions/{session_id}/turn-operations/{operation_id}` applies the same
access check and reconciles runner status when the bound runner is reachable.
If it is not reachable, the durable journal projection remains readable.

The endpoint deliberately rejects transcript-owned native terminal sessions in
v1alpha1. Those sessions make the native transcript bridge the canonical input
writer, so server-side deterministic item persistence would violate their
single-writer invariant. The first public slice supports server-persisted SDK
and non-native harness turns only.
