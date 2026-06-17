# Phase 1 — Server-Authoritative Turns ("Stop the Bleeding")

> **PROPOSAL.** Implementation plan for Phase 1 of
> [`PAUSE_RESUME_DISCONNECT_TOLERANCE.md`](./PAUSE_RESUME_DISCONNECT_TOLERANCE.md).
> Tracking issue: #466. Design PR: #461.
>
> This plan is written against the actual codebase (head migration
> `m1a2b3c4d5e6`), not a greenfield design. Phase 0 (unlink turn cancellation
> from socket close; client auto-attach on "already processing") is assumed
> shipped.

## Scope

Phase 1 ships as **one release** and delivers the foundation that the rest of
the project stacks on:

1. **Server-authoritative `Turn` state machine** owned by a runner-side
   supervisor under a **lease + heartbeat**. The HTTP/SSE handler only
   *observes*; a client disconnect at most flips `attached` / `last_client_seen`
   and may **never** transition a `RUNNING` turn.
2. **Idempotency keys** (client-generated UUIDv7) on mutating sends + a
   structured **`409 Attach-Required`** response replacing the "Agent is
   already processing" error, with an explicit send **intent**
   (`enqueue` / `steer` / `attach`).
3. **Failure taxonomy** stored on the turn record, with the inviolable rule
   that **only `WORKER_*` classes feed vendor health/routing**.

Out of scope (later phases): durable inbox outbox (Phase 2), pause/resume
control surface (Phase 3), tool-boundary checkpointing (Phase 4), full event
log (Phase 5). The `PAUSING`/`PAUSED` states and the `checkpoint_id` column are
introduced now as forward-compatible no-ops to avoid a later migration.

## Codebase grounding (verified)

| Assumption | Verified in repo |
|---|---|
| Stack: FastAPI + SQLAlchemy 2.0 (`DeclarativeBase`/`Mapped`), Alembic | `omnigent/db/db_models.py`, `omnigent/db/migrations/` |
| Migrations head | `m1a2b3c4d5e6` (`alembic heads`) |
| Dual-dialect SQLite + Postgres via `sqlite_where`/`postgresql_where` | `db_models.py` partial indexes (e.g. `ix_agents_template_name`) |
| Timestamps are integer epoch-seconds | `db.utils.now_epoch()` |
| A "turn" already half-exists as the response id | `generate_task_id()` → `resp_<hex>`; `conversation_items.response_id` |
| Runner affinity already persisted | `conversations.runner_id` (hard affinity) |
| Migrations use `op.batch_alter_table` (SQLite-safe) | every recent revision |

**Key decision:** `turn_id` **is** the existing response/task id (`resp_…`).
We promote a latent concept to a first-class row rather than inventing a
parallel id space. `generate_task_id()` keeps minting it.

## (a) Data model

Three changes: new `turns` table, new `idempotency_keys` table, one column on
`conversations`. All additions are nullable or carry `server_default`, so the
migration is online-safe (no table rewrite).

### `turns`

| Column | Type | Notes |
|---|---|---|
| `id` | `String(64)` PK | `turn_id` == `resp_…` response/task id |
| `conversation_id` | `String(64)` FK→`conversations.id` CASCADE | |
| `status` | `String(16)` default `CREATED` | `CREATED·QUEUED·RUNNING·PAUSING·PAUSED·COMPLETED·FAILED·CANCELLED` |
| `error_code` | `String(32)` null | taxonomy value (§f) |
| `error_message` | `Text` null | human-readable detail |
| `vendor` | `String(32)` | `claude_code·codex·pi·…` |
| `intent` | `String(16)` | `enqueue·steer·attach` |
| `input_json` | `Text` | the send payload, durable **before** work starts |
| `lease_owner` | `String(64)` null | runner_id; runner-held, never client-held |
| `lease_epoch` | `Integer` default `0` | monotonic **fencing token** |
| `last_heartbeat_at` | `Integer` null | epoch seconds |
| `lease_expires_at` | `Integer` null | `last_heartbeat_at + TTL` |
| `attached` | `Boolean` default `false` | client liveness; **never** gates execution |
| `last_client_seen` | `Integer` null | |
| `created_at` | `Integer` | `now_epoch()` |
| `start_ts` | `Integer` null | set on → `RUNNING` |
| `end_ts` | `Integer` null | set on terminal |
| `checkpoint_id` | `String(64)` null | **Phase 4 forward-compat; always NULL in Phase 1** |

Constraints / indexes:

- `CheckConstraint` on `status` and on `error_code` (closed value sets).
- `Index ix_turns_live_lease (lease_expires_at)` **partial** on
  `status IN ('RUNNING','PAUSING')` — the orphan-sweep hot path.
- `Index ix_turns_conversation_created (conversation_id, created_at DESC)`.
- **`Index ux_turns_one_active_per_conversation (conversation_id) UNIQUE`
  partial** on the non-terminal statuses. This makes "already processing" a
  **database invariant**: a racing dispatch hits `IntegrityError`, which the
  API converts to `409 Attach-Required`. Same pattern as the existing
  `parent_title` partial-unique index.

> **Flag for review:** the partial-unique index enforces **queue depth 1** per
> conversation (one runner per conversation, true today via `runner_id`). If we
> ever want depth > 1, this index becomes non-unique and ordering moves to a
> `queue_seq` column. Recommend keeping depth-1 for Phase 1.

### `idempotency_keys`

| Column | Type | Notes |
|---|---|---|
| `key` | `String(36)` PK | client-generated UUIDv7, stored raw |
| `conversation_id` | `String(64)` FK CASCADE | |
| `turn_id` | `String(64)` FK→`turns.id` CASCADE | the turn this key created/returned |
| `request_fingerprint` | `String(64)` | sha256 of canonicalized payload |
| `created_at` | `Integer` | |

- PK on `key` ⇒ dedup is insert-or-conflict. Replay returns the **same**
  `turn_id`.
- `request_fingerprint` detects "same key, different body" → reject with `422`
  rather than silently returning the wrong turn.
- **Retention:** cascade-deleted with the conversation, plus a cheap daily TTL
  sweep (`created_at < now − 7d`). Deferred-but-cheap cron, not Phase-1
  blocking.

### `conversations` (one column)

`default_send_intent String(16) NULL` — `enqueue·steer·attach`; `NULL` ⇒
resolve from policy default (§e).

### Migration

One revision `n1a2b3c4d5e6_phase1_server_authoritative_turns`,
`down_revision = "m1a2b3c4d5e6"`, using `op.batch_alter_table` for the
`conversations` column add and `op.create_table` for the two new tables.
Online-safe on Postgres; SQLite-safe via batch mode.

## (b) Turn state machine

```
              enqueue dispatch (server)
   CREATED ─────────────────────────────► QUEUED
      │                                      │ supervisor claims lease
      │ interactive/attach dispatch (server) ▼
      └────────────────────────────────►  RUNNING ──► COMPLETED   (supervisor)
                                            │  ├─────► FAILED      (supervisor)
                                            │  └─────► CANCELLED   (supervisor)
                                            │ pause-at-boundary (Phase 4; no-op hook now)
                                            ▼
                                         PAUSING ──► PAUSED
```

| From → To | Trigger | Who |
|---|---|---|
| ∅ → `CREATED` | dispatch row persisted, pre-forward | **Server** API handler (persist-before-forward txn) |
| `CREATED` → `QUEUED` | accepted into a runner queue | **Server** dispatch path |
| `QUEUED` → `RUNNING` | supervisor acquires lease + begins | **Runner supervisor** only |
| `RUNNING` → `COMPLETED` | worker returned a result | **Runner supervisor** only |
| `RUNNING` → `FAILED` | worker boot/task error | **Runner supervisor** only |
| `RUNNING` → `CANCELLED` | explicit user abort | **Runner supervisor**, on server cancel intent |
| `RUNNING` → `PAUSING` → `PAUSED` | cooperative pause | **Runner supervisor** (Phase 4; wired no-op now) |
| non-terminal → `FAILED(RUNNER_LOST)` | lease expiry | **Server orphan sweeper** — the *only* server write to a leased turn |
| any → `attached`/`last_client_seen` | client connect/disconnect | **Server** (observation only, never `status`) |

**Two inviolable rules, enforced in code (not convention):**

1. The HTTP/SSE handler may only write `attached` and `last_client_seen`.
   Enforce via a dedicated store method `mark_client_liveness(...)` whose
   `UPDATE … SET` clause contains *only* those two columns, with a unit test
   asserting the emitted SQL touches nothing else.
2. Only the lease holder may advance `RUNNING → terminal`. Every supervisor
   write is a **fenced compare-and-set**:
   `UPDATE turns SET status=:new WHERE id=:tid AND lease_owner=:me AND lease_epoch=:epoch AND status=:expected`.
   Zero rows updated ⇒ the supervisor lost the lease; it must abort, not retry.

## (c) Lease + heartbeat

- **Lease owner** of a `RUNNING` turn is the runner pinned via
  `conversations.runner_id`. We add liveness/ownership on top of the existing
  binding — not a new placement system.
- **Fencing** via `lease_epoch` (monotonic int). Every heartbeat and terminal
  write carries the epoch; CAS keeps single-writer semantics even after a brief
  runner blip.
- **Defaults (config-tunable):** heartbeat interval **5s** (±1s jitter), lease
  TTL **30s** (≥ 6× interval to tolerate GC pauses / CPU spikes), orphan sweep
  cadence **10s**. Tune at rollout.
- **Independent heartbeat (the critical footgun fix):** the heartbeat runs as a
  dedicated runner-side **background asyncio task** (the runner is a long-lived
  process per the WS tunnel registry), on a **separate DB session** from the one
  the turn's tool calls use. A blocking tool call therefore cannot starve the
  heartbeat → no false `LeaseExpired`. Heartbeat update:
  `UPDATE turns SET last_heartbeat_at=:now, lease_expires_at=:now+TTL WHERE id=:tid AND lease_owner=:me AND lease_epoch=:epoch` — `rowcount==0` ⇒ stop (lease lost).
  - *Heartbeat transport variant:* if a deployment's runner cannot reach the DB
    directly, the heartbeat write proxies over the WS tunnel to the server,
    which performs the same CAS. Design is unchanged.
- **Orphan detection:** server sweeper selects
  `status IN ('RUNNING','PAUSING') AND lease_expires_at < now()` (uses the
  partial index) and CAS-transitions each to `FAILED(error_code=RUNNER_LOST)`.
- **Phase-1 `RUNNER_LOST` recovery = surface, do not auto-resume.**
  Resume-from-checkpoint isn't built until Phase 4, and we must never
  double-execute a non-idempotent vendor turn. The orchestrator/human sees the
  `RUNNER_LOST` failure and may explicitly enqueue a follow-up.

## (d) API surface

Common headers on mutating POSTs:
- `Idempotency-Key`: UUIDv7 (see §g for rollout leniency).
- `X-Send-Intent` *(optional)*: `enqueue` | `steer` | `attach`; resolved
  per-session (§e) when absent.

Endpoints:

- **`POST /v1/sessions/{session_id}/turns`** — create/enqueue.
  - Body: `{ vendor, input, metadata?, intent? }`.
  - Idempotency: look up `(conversation_id, key)`. Hit + matching fingerprint →
    return stored `turn_id` with `Idempotent-Replay: true`. Hit + different
    fingerprint → `422`. Miss → insert key + turn in one txn.
  - Racing a live turn (intent `enqueue`/`attach`) → caught `IntegrityError`
    from the partial-unique index → **`409 Attach-Required`**.
  - `202 Accepted` → `{ turn_id, status: "QUEUED", … }`.
- **`GET /v1/turns/{turn_id}`** — full turn record (status, error_code, timing,
  `attached`).
- **`GET /v1/turns/{turn_id}/stream`** (SSE) — observe. On client disconnect:
  set `attached=false`, `last_client_seen=now` — **no lifecycle change.**
- **`POST /v1/turns/{turn_id}/cancel`** — best-effort cooperative cancel;
  `RUNNING → CANCELLED(error_code=CANCELLED)`.

**`409 Attach-Required` contract** (a control response, not an error):

```json
{
  "code": "ATTACH_REQUIRED",
  "message": "A turn is already running for this session",
  "turn_id": "resp_…",
  "status": "RUNNING",
  "attach_url": "/v1/turns/resp_…/stream",
  "follow_url": "/v1/turns/resp_…",
  "steer_supported": true
}
```

## (e) Per-session intent default policy

- Resolution order: explicit `intent` in request → `X-Send-Intent` header →
  `conversations.default_send_intent` → **system default by session kind**.
- System defaults: **orchestrator/programmatic dispatch → `enqueue`**;
  **interactive human session → `attach`**. Session kind is derived from the
  existing session metadata (sub-agent vs. root/human).
- `default_send_intent` lets a session pin an override (set at session create,
  read on dispatch).

## (f) Failure-taxonomy plumbing

- `error_code` is set **only** at a terminal transition, by the actor that owns
  it: the **runner supervisor** sets `WORKER_BOOT_FAILURE` / `WORKER_TASK_FAILURE`
  / `CANCELLED`; the **server sweeper** sets `RUNNER_LOST`; the transport layer
  sets `TRANSPORT_DISCONNECT` only on the observation path (never on a `RUNNING`
  turn's status).
- **Inviolable routing rule:** the vendor health/routing input
  (`runner/routing.py`) consumes failures **filtered to `WORKER_*` only**. A
  single helper `is_worker_attributable(error_code) -> bool` gates it; a unit
  test asserts `TRANSPORT_DISCONNECT` and `RUNNER_LOST` are excluded. This is
  the fix for the original misdiagnosis (benching a healthy vendor on transport
  noise).

## (g) Rollout / migration

- **Migration** adds tables + column; backfills nothing structural (existing
  in-flight responses simply won't have turn rows — acceptable, see below).
- **Feature flag** `server_authoritative_turns` (SAFE flag) gates the new
  dispatch path. Off → legacy connection-coupled path unchanged.
- **Dual-path during ramp:** when the flag is on, dispatch writes a `turns` row
  and routes through the supervisor/lease path; when off, the old path runs. No
  client is required to send `Idempotency-Key` while the flag is ramping — the
  server mints one server-side if absent (logged as a deprecation) so old
  clients keep working. Tighten to required after clients update.
- **Backfill:** in-flight legacy responses at cutover are *not* retrofitted into
  `turns`; they drain on the old path. New dispatches under the flag get turn
  rows. This avoids a risky live backfill.
- **Rollback:** flag off restores the legacy path instantly; the new tables are
  inert when unused.

## (h) Test plan

- **Unit:** state-machine transition matrix (all illegal transitions rejected);
  `mark_client_liveness` SQL touches only the two columns; fenced-CAS rejects a
  stale-epoch writer; `is_worker_attributable` excludes transport/runner codes;
  idempotency replay returns same `turn_id`; same-key-different-body → 422.
- **Integration:** dispatch → `QUEUED → RUNNING → COMPLETED`; racing second
  dispatch → `409 Attach-Required`; cancel → `CANCELLED`.
- **The disconnect repro (the headline test):** start a turn with a multi-second
  fake tool call; drop the SSE client mid-turn; assert the turn stays `RUNNING`,
  the heartbeat keeps extending the lease, the turn reaches `COMPLETED`, and
  `attached` flipped to false without any status change.
- **Heartbeat-starvation test:** make the tool call block the execution task for
  > TTL; assert the independent heartbeat task still extends the lease and the
  turn is **not** swept as `RUNNER_LOST`.
- **Orphan-sweep test:** kill the heartbeat (simulate runner crash); assert the
  sweeper transitions the turn to `FAILED(RUNNER_LOST)` after TTL and that this
  failure does **not** affect vendor routing.

## (i) Observability / metrics

- Counters: turns by `status` and `error_code`; `409 Attach-Required` rate;
  idempotency replays; `422` fingerprint mismatches; orphan-sweep
  `RUNNER_LOST` count.
- Gauges: live leased turns; oldest `lease_expires_at` lag (sweep health).
- Histograms: heartbeat write latency; queue→running wait; turn duration.
- **Alert:** spike in `RUNNER_LOST` (infra health) — and explicitly assert it is
  decoupled from vendor health dashboards, so transport noise never pages as a
  vendor outage.

## Risks / flags for review

- **Partial-unique-index = queue depth 1.** Intentional for Phase 1; revisit for
  depth > 1 (§a).
- **Shared DB assumption** for the heartbeat write — confirm per deployment;
  fallback is the tunnel-proxied variant (§c).
- **Server-minted idempotency keys during ramp** weaken idempotency for old
  clients; acceptable transitional state, tighten post-ramp (§g).
- **Clock skew** between runner and server affects lease expiry; prefer a single
  DB-server clock (`now()` in SQL) for both heartbeat and sweep comparisons to
  avoid cross-host skew.

## Build order within Phase 1 (sub-PRs)

This plan ships as one release but is reviewable as a stack:

1. **Schema** — `SqlTurn` + `SqlIdempotencyKey` models + migration + model unit
   tests. *(No behavior change; safe to merge first.)*
2. **State machine + lease/heartbeat** — supervisor ownership, fenced CAS,
   independent heartbeat task, orphan sweeper.
3. **API + idempotency + `409 Attach-Required`** — dispatch path, intent
   resolution, structured error.
4. **Failure taxonomy + routing filter** — `error_code` plumbing +
   `is_worker_attributable` gate.
5. **Feature-flag wiring + rollout** + the disconnect/starvation/orphan tests.
