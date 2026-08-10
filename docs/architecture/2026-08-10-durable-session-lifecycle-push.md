# OMN-104 — Durable Session Lifecycle Push to Manager Webhook

Status: design-first, decision-ready. No product code in this change.
Branch: `design/omn-104-durable-session-push`. Base: `origin/main` @ `b5adc79e8a76a41ffe4ca09799dc852dfc7ca7f5` (2026-08-10).
Linear: [OMN-104](https://linear.app/silica-v1/issue/OMN-104/push-durable-omnigent-session-completion-and-human-decision-events-to)

This document was produced with Debby's two-head debate process (Claude + GPT,
independently grounded in the current tree, two rounds: opening positions +
one cross-critique round). Both heads' opening positions disagreed sharply on
whether a durable elicitation table was needed (§5.4) — and, after critique,
fully converged on building it, with a sharper mechanistic justification
(two separate in-memory registries, server- and runner-side) than either
heads' opening answer had. The critique round also caught and corrected a
real design error (§5.1's now-removed `background_task_count` gate) and an
incorrect base-class attribution in the original grounding brief (§2.4).
That process — not just the final answer — is why this went through a full
debate rather than a single pass: both corrections were caught by one head
re-verifying a citation against the live tree rather than trusting the other
head's or the brief's claim at face value.

All file:line citations below were read directly from the current tree
(`git rev-parse HEAD` == `origin/main` == `b5adc79e8a76a41ffe4ca09799dc852dfc7ca7f5`,
verified live, not from README/comments/memory — see `source-of-truth`
policy). **A stale-base grounding pass on this same task (HEAD `7a519e49`,
1177 commits behind) was discarded in full** after the mismatch was caught
and verified; nothing from that pass survives into this document. The tree
was mid-refactor across those 1177 commits: the former monolithic
`omnigent/server/routes/sessions.py` no longer exists — it now splits into
`omnigent/server/routes/sessions/` (route handlers) and
`omnigent/server/routes/_sessions/` (`common.py`, `helpers.py`,
`orchestration.py`), and `omnigent/db/db_models.py` split conversation content
(`ConversationBase`) from Omnigent operational state (`OmnigentBase`).

---

## 1. Objectives and acceptance mapping

Product decision (already approved, not re-litigated here): push
`session.completed` / `session.failed` / `session.awaiting_decision` /
`session.resumed` to a configured manager webhook, authenticated and durable,
with poll/reconciliation retained as the correctness backstop.

| # | Acceptance requirement (from OMN-104) | Where this document satisfies it |
|---|---|---|
| 1 | Persist event before delivery (transactional outbox); retry until 2xx with bounded backoff | §4 (outbox schema/transaction), §7 (dispatcher/retry) |
| 2 | HMAC-sign requests; configurable endpoint + secret | §8 |
| 3 | At-least-once, stable event IDs, consumer dedupes | §4 (deterministic IDs), §6 |
| 4 | Per-session ordering; delayed events cannot regress manager state | §6 |
| 5 | Attempts/outbox status in API and logs | §9 |
| 6 | Callback delivery never blocks or changes session terminal transitions | §5, §7 |
| 7 | Poll/reconcile stays enabled | §11 |
| 8 | Redact secrets and raw environment values | §3 (redaction boundary) |
| 9 | Integration coverage: completion wake, decision payload/context, restart/network replay, duplicate delivery/stable IDs, signature/redaction security, end-to-end decision response resuming the exact session | §12 (test matrix, one row per bullet) |

---

## 2. Current code paths (ground truth, current tree)

### 2.1 Session status — partially durable, single chokepoint

- In-memory, per-replica cache: `_session_status_cache: dict[str, str] = {}`
  — `omnigent/server/routes/_sessions/common.py:396`.
- Every status transition funnels through one function —
  `_publish_status()` — `omnigent/server/routes/_sessions/helpers.py:3548`.
  Its own docstring: "every publish site funnels through here so the
  in-memory `_session_status_cache` stays coherent with the SSE stream."
  Four literal states only: `{"idle", "running", "waiting", "failed"}`
  (`orchestration.py:937`). There is no first-class `completed`,
  `awaiting_decision`, or `resumed` state anywhere in the codebase — these
  are the SSE/UI vocabulary, not the manager-webhook vocabulary.
- Sticky-failed guard: a trailing `idle` after `failed` is dropped rather
  than overwriting the cache — `helpers.py:3595`. This is the codebase's
  existing precedent for "don't fire on a quiescence blip."
- **New since the earlier (stale) grounding pass**: `_publish_status` now
  also durably mirrors the transition to
  `omnigent_conversation_metadata.live_status` (SmallInteger, nullable) via
  `omnigent/server/session_live_state.py:persist_live_status()`, called at
  `helpers.py:~3605`. Added by migration
  `omnigent/db/migrations/versions/d7f1a2b3c4e5_add_conversation_metadata_live_state.py`;
  encoded via `SESSION_LIVE_STATUS = {"idle":1,"running":2,"waiting":3,"failed":4}`
  (`omnigent/db/enum_codecs.py:71-76`). This mirror is **explicitly
  best-effort** — `session_live_state.py`'s module docstring: "a failed
  write logs and is dropped; live state is display state, and the next
  transition rewrites it." It is not a durability guarantee and must not be
  treated as one for this feature (both debate heads independently reached
  this conclusion — see §5).
- Failure detail (the only pre-existing durable *content*, as opposed to
  state) is persisted via `_persist_session_status_error_labels()` into the
  `conversation_labels` upsert table, read back as `last_task_error`.

### 2.2 The load-bearing precedent: `persist_scheduled_run_completion`

`omnigent/server/session_live_state.py` already implements, for a sibling
feature (scheduled tasks), almost exactly the "derive terminal completion
from `_publish_status`" mechanism OMN-104 needs:

> "called from `_publish_status` wherever a session reaches a durable
> terminal edge (`idle` = the turn completed, `failed` = it errored/
> disconnected)."

It writes to `SqlScheduledTaskRun` (`db_models.py:1502`, `OmnigentBase`,
columns `status`/`error`/`error_code`/`fired_at`/`finished_at`), is
idempotent via a conditional `UPDATE ... WHERE status = 'running'`, and its
durability backstop is a **separate, deliberately lazy-on-read reconciler**:
`omnigent/server/scheduled/run_reconciler.py` force-fails any run still
`running` past `STALE_RUN_MAX_AGE_SECONDS` (6h), checked only when a user
reads the scheduled-tasks list/detail endpoints. Its docstring is explicit
that this was a deliberate choice against both a startup sweep and a
periodic poll, because "the event hook handles every normal run, and
lazy-on-read reconciles anything a user actually views" — i.e., correct only
because an orphaned scheduled-run status is harmless until someone looks.
This precedent's own justification is also its own scope limit (§5, §7).

### 2.3 Elicitation / decision state — still fully in-memory for content

- `omnigent/server/_elicitation_registry.py:15`:
  `_harness_elicitation_registry: dict[str, asyncio.Future[ElicitationResult]]`
  — dies with the process. Populated at
  `omnigent/server/routes/_sessions/orchestration.py:416`
  (`_harness_elicitation_registry[elicitation_id] = future`).
- `omnigent/runtime/pending_elicitations.py` is a second in-memory index,
  populated through the single SSE publish chokepoint —
  `omnigent/runtime/session_stream.py:131`
  (`pending_elicitations.record_publish(conversation_id, event)`) — on every
  `response.elicitation_request` / `response.elicitation_resolved` event.
  Its own docstring: "when a shared backplane is added for the registry,
  this index should be wired through the same backplane" — the codebase
  already names this as future, out-of-this-scope work.
- **New since the stale pass**: the *count* of outstanding elicitations is
  now durable and cross-replica —
  `omnigent_conversation_metadata.pending_elicitation_count`, written by
  `session_live_state.persist_pending_count()`. The *content* (what was
  asked, what was decided) is not persisted anywhere. So today: "is this
  session waiting on a human" survives a restart; "what were they asked and
  what did they decide" does not.
- `_harness_pre_resolved_elicitations` (`_elicitation_registry.py`) is an
  in-process race tombstone for a narrow timing window (web verdict arrives
  before the harness hook re-parks) — not a cross-restart mechanism.

### 2.4 DB/schema conventions

- `omnigent/db/db_models.py` declares two separate declarative bases:
  `OmnigentBase` (operational/non-conversation tables) and `ConversationBase`
  (conversation-scoped content). `SqlConversation` (line 745) is on
  `ConversationBase`; `SqlConversationMetadata` (line 601) — which holds
  `live_status` and `pending_elicitation_count`, i.e. exactly the state this
  feature extends — is on **`OmnigentBase`**, not `ConversationBase`
  (confirmed by reading the class declaration directly; this correction
  surfaced mid-debate, see §5). The two bases exist so they can be routed to
  **separate physical database engines** via `conversation_storage_location`
  (`db_models.py:174-191`) — this is load-bearing for §4's base choice.
- Every table carries `workspace_id: BigInteger` as part of a composite
  primary key (multi-tenant Databricks partitioning); no exceptions.
- Enum-like columns are stable, append-only `SMALLINT` codes translated at
  the store boundary — `omnigent/db/enum_codecs.py`. `SqlScheduledTaskRun` /
  `SqlScheduledTask` are the closest existing shape to "an attempt/run row
  with an outcome," both on `OmnigentBase`.
- `omnigent/stores/conversation_store/sqlalchemy_store.py:756-757`: two
  separate dialect-aware lock flags,
  `self._supports_for_update` (conversation engine) and
  `self._meta_supports_for_update` (metadata engine), both
  `dialect.name != "sqlite"`. `.with_for_update()` is the established
  row-lock idiom (e.g. `_lock_conversation`, used for `conversation_items`
  position allocation). **No `SKIP LOCKED` usage exists anywhere in the
  codebase today** — introducing it is a new pattern, justified in §7.
  Multi-replica server deployment is explicitly real (this is *why*
  `live_status`/`pending_elicitation_count` were added as cross-replica
  mirrors in the first place — `session_live_state.py`'s own module
  docstring says so).

### 2.5 Other precedents reused below

- Background-task lifespan pattern: `omnigent/server/app.py:933`
  `@asynccontextmanager` lifespan; `asyncio.create_task` at `app.py:1052`
  for `metrics_publish_task`; cancel + `await` with
  `suppress(asyncio.CancelledError)` on shutdown (`app.py:~1124`).
- Secrets convention: `omnigent/server/server_config.py` — secrets
  (`DATABASE_URL`, cookie secret, OIDC client secret) come from environment
  variables only, never the mounted YAML config file (explicit 12-factor
  rationale in the module docstring); non-secret settings go in
  `<data_dir>/config.yaml`.
- Inbound-only HMAC precedent: `omnigent/server/oidc.py:105
  hmac_digest(token, secret)` (HMAC-SHA256, used for a credential-cache
  key); `omnigent/inner/egress/proxy.py` uses `hmac.compare_digest` for
  constant-time header comparison. No outbound request-signing code exists
  to extend — §8 proposes one from scratch.
- Redaction precedent: `omnigent/runtime/telemetry.py:124
  _REDACT_KEY_SUBSTRINGS = ("token", "secret", "password", "authorization",
  "credential", "api_key", "apikey")` with a `_redact_payload()` helper.
- Retry precedent: `omnigent/runtime/llm_retry.py` and
  `omnigent/runtime/tool_retry.py` — two independent exponential-backoff
  implementations against `omnigent.spec.types.RetryPolicy`; neither is
  shared today, so there is no single retry module this feature is
  obligated to extend.
- Read-endpoint precedent for exposing attempt/status history:
  `GET /scheduled-tasks/{scheduled_task_id}/runs`
  (`omnigent/server/routes/scheduled_tasks.py:380`).
- Wire-event schema precedent: `SessionStatusEvent`
  (`omnigent/server/schemas.py:2590`).

---

## 3. Event schema, versioning, redaction boundary

### 3.1 Envelope

```json
{
  "event_id": "evt_<uuid5-hex>",
  "event_version": 1,
  "event_type": "session.completed",
  "workspace_id": 0,
  "session_id": "conv_...",
  "sequence": 42,
  "occurred_at": 1770000000,
  "data": { "...": "event-type-specific" }
}
```

- `event_id` — deterministic (UUIDv5 over a namespace + `workspace_id` +
  `conversation_id` + `event_type` + `transition_key`), not a random UUID,
  so a retried producer call is naturally idempotent at the DB PK rather
  than needing a separate dedupe query (both heads converged on this
  independently).
- `event_version` — starts at `1`; a breaking payload change bumps this,
  never silently changes `data`'s shape under the same version.
- `sequence` — per-`(workspace_id, session_id)` monotonic integer; see §6.
- `data` shape per `event_type`:
  - `session.completed`: `{ "response_id": "..." }` (informational
    `background_task_count` may be included from the same `_publish_status`
    call if non-null, but it is never a gating condition — see §5.1)
  - `session.failed`: `{ "response_id": "...", "reason": { "code": "...", "message": "..." } }`
    (reason is the *same* `ErrorDetail` object already passed into
    `_publish_status` — not re-derived from the `conversation_labels`
    write, which would be a second formatter of the same fact)
  - `session.awaiting_decision`: `{ "elicitation_id": "...", "request": {...allowlisted...} }`
  - `session.resumed`: `{ "elicitation_id": "...", "decision": {...allowlisted...} }`

### 3.2 Redaction boundary

Two distinct redaction rules, not one, because the two payload classes have
different authorization boundaries:

1. **`session.completed` / `session.failed`** — no user/environment content
   by construction (`response_id`, an `ErrorDetail.code`/`message` pair).
   Still run `ErrorDetail.message` through the existing
   `_REDACT_KEY_SUBSTRINGS` key-based filter (`telemetry.py:124`) *and* a
   value-pattern check reusing `_truncate_label`'s length clamp
   (`helpers.py`) before insert, since a harness-originated error message
   can echo back arbitrary tool output.
2. **`session.awaiting_decision` / `session.resumed`** — the payload is a
   manager-authorized *decision* context, not free telemetry, so it is NOT
   blanket-redacted (a blanket redaction would violate the acceptance
   requirement that the manager receive real decision payload/context).
   Instead: an explicit **field allowlist** derived from the elicitation
   request schema (prompt, choices, requesting tool/action name,
   correlation IDs) is serialized into `request_payload`/`decision_payload`;
   nothing else is copied in. Raw environment dictionaries, process
   arguments, headers, and secrets are never eligible fields regardless of
   key name — enforced by construction (allowlist, not denylist) rather
   than by `_REDACT_KEY_SUBSTRINGS` alone, which is a denylist and would
   silently pass through anything it doesn't recognize.

Logs (§9) always use the denylist-style `_REDACT_KEY_SUBSTRINGS` filter,
regardless of which rule produced the payload, and never log the payload
body, the signature, the secret, or the target URL unredacted.

---

## 4. Transactional boundary, outbox schema, indexes

### 4.1 Base: `OmnigentBase`, not `ConversationBase`

The outbox table lives on `OmnigentBase`, matching `SqlScheduledTaskRun` and
`SqlConversationMetadata` (§2.4). This is required, not stylistic: under a
`conversation_storage_location` split-engine deployment, a `ConversationBase`
table and an `OmnigentBase` table are on two different physical databases —
writing the outbox row on `ConversationBase` while the state it reports on
(`live_status`, elicitation registration) lives on `OmnigentBase` would
reintroduce exactly the dual-write inconsistency the outbox pattern exists
to prevent. The outbox insert must be transactional with the fact it is
reporting, and that fact already lives on `OmnigentBase`.

Because the outbox is on a different engine than `conversations`/
`conversation_items` (`ConversationBase`), `_lock_conversation`
(`sqlalchemy_store.py`) cannot be reused to allocate `sequence` — it locks
through the *conversation* engine. Instead:

```
session_lifecycle_cursors   (OmnigentBase)
  workspace_id   BigInteger  -- PK member
  session_id     Uuid16      -- PK member
  next_sequence  BigInteger  -- next value to assign
```

`sequence` is allocated by locking this cursor row
(`.with_for_update()` where `_supports_for_update`-equivalent on the
metadata engine, single-writer on SQLite — same dialect branch already used
for the metadata engine, not a new locking convention) and inserting the
outbox row, in one transaction, on the metadata engine.

### 4.2 `session_lifecycle_outbox` (OmnigentBase)

| Column | Type | Notes |
|---|---|---|
| `workspace_id` | BigInteger | PK member |
| `id` | Uuid16 | PK member; the externally stable `event_id` |
| `session_id` | Uuid16 | indexed; no DB FK (Rule R032, app-owned like `scheduled_task_runs.conversation_id`) |
| `event_type` | SmallInteger | new append-only `enum_codecs` table: `{"session.completed":1,"session.failed":2,"session.awaiting_decision":3,"session.resumed":4}` |
| `transition_key` | String(192) | stable source identity, see §5.4 |
| `sequence` | BigInteger | from `session_lifecycle_cursors`, monotonic per `(workspace_id, session_id)` |
| `event_version` | SmallInteger | default 1 |
| `payload` | CompressedText | JSON per §3; redacted/allowlisted before insert |
| `status` | SmallInteger | `{"pending":1,"leased":2,"delivered":3,"dead_letter":4,"paused":5}` + CHECK |
| `attempt_count` | Integer | default 0 |
| `next_attempt_at` | Integer | epoch seconds; dispatcher claim filter |
| `lease_owner` / `lease_expires_at` | String / Integer, nullable | replica identity + stuck-claim recovery |
| `last_attempt_at` / `delivered_at` | Integer, nullable | |
| `last_http_status` | SmallInteger, nullable | |
| `last_error_code` / `last_error_message` | String(64) / String(256), nullable | sanitized only, never raw body |
| `created_at` | Integer | |

Indexes / constraints:

- Unique `(workspace_id, session_id, sequence)` — ordering + prevents
  double-allocation.
- Unique `(workspace_id, session_id, event_type, transition_key)` — the
  idempotency key; a retried producer call that races the cursor allocation
  resolves to the existing row instead of a duplicate.
- Claim index `(status, next_attempt_at, workspace_id)` — the dispatcher's
  hot query.
- Per-session ordering/head lookup `(workspace_id, session_id, sequence,
  status)`.

### 4.3 Producer transaction shape

The INSERT into `session_lifecycle_outbox` (cursor lock + sequence
allocation + row insert) happens **synchronously, in the same transaction**
as the fact being reported (see §5.4 for exactly which write that is per
event type) — not backgrounded through `session_live_state.py`'s
fire-and-forget executor. That module's own docstring states its writes are
best-effort and self-healing on the next transition; a lifecycle event has
no "next transition" that repairs a silently dropped one, so it needs the
opposite durability posture. This is why the outbox write is a **new,
separate module** (`omnigent/server/session_outbox.py`, parallel to but not
merged into `session_live_state.py`) rather than added to the existing
best-effort mirror — sharing a module invites a future edit copy-pasting the
fire-and-forget pattern onto a call site that requires durability.

Cost: the four gated transitions (§5) now pay a synchronous DB write inside
the request/hook path that previously only wrote an in-memory dict. Every
other `_publish_status` call (the common case — plain `running` mid-turn)
pays nothing extra, since it doesn't match any of the four gates.

What happens if that synchronous write fails is call-site-dependent, not
uniform, verified against the two shapes `_publish_status` is actually
called from:

- **Call sites that originate from a POST handler with existing ack
  semantics** — e.g. `routes_events.py`'s relay/hook endpoints (which
  already return a small acknowledgement body per their own
  `response_model=None` handler comments, around `routes_events.py:239`) —
  withhold the 200 and return a retryable non-2xx on an outbox-write
  failure, requiring the runner/relay to replay its stable event. This
  reuses ack semantics that already exist at those call sites rather than
  inventing a new protocol, conditional on the calling runner's HTTP client
  actually retrying on a non-2xx (verify/add this at implementation time).
- **Internal call sites with no network round trip of their own**
  (disconnect handling, snapshot reconstruction, native-forwarder code in
  `orchestration.py`/`helpers.py`) — let the write's exception propagate
  uncaught into whatever request *did* originate that call chain, and let
  that outer request's own existing failure/retry semantics handle it. No
  new protocol needed here; this is the plain "synchronous,
  error-propagating" behavior, correctly scoped to where it's sufficient.

---

## 5. Transition → event mapping, idempotency, terminal-reason derivation

### 5.1 `session.completed` / `session.failed` — bind to `_publish_status`

Both debate heads converged on binding to an **edge**, not a level-read of
`status`, to avoid over-firing on quiescence blips (the existing sticky
`failed`→`idle` guard at `helpers.py:3595` already demonstrates why a level
read is wrong):

- `session.completed`: fires when `_publish_status` accepts a transition
  **into** `idle` while `_session_active_response_cache.get(session_id)` is
  not `None` (a turn was actually open) — read that value *before* the
  existing cache `.pop()` a few lines later, and use it as `response_id` /
  the transition key. A session that never opened a turn, or an `idle`→`idle`
  no-op write, has no cached `response_id` and correctly emits nothing.
  **No secondary gate on `background_task_count`/`blocked_on`.** An earlier
  draft of this design proposed additionally gating on
  `background_task_count in (None, 0)`, reasoning that a lingering
  background shell means the turn isn't "really" done. That gate is wrong,
  verified against `_background_task_delivery_status()`
  (`helpers.py:3164-3199`): its own docstring states it deliberately
  collapses a claude-native `waiting` back to `idle` *specifically because*
  "the turn itself is over... deliver idle and let the count speak for the
  shells." `_publish_status` is called with `idle` only once that
  resolution has already happened upstream — gating on the count again
  inside the outbox hook would silently swallow `session.completed` for
  every claude-native session that ends its turn with a dev-server/watcher
  still running, which is the common case, not an edge case. Trust the
  status string `_publish_status` was called with; do not re-derive
  "is it really done" a second time from the same parameters a caller
  already resolved it from.
- `session.failed`: fires on any transition **into** `failed` (not
  swallowed by the sticky guard), using the `ErrorDetail` parameter already
  passed to `_publish_status` verbatim as the `reason` (§3.1) — not
  re-derived from `_persist_session_status_error_labels()`'s
  `conversation_labels` write, which is a second, independent consumer of
  the same fact and must not become a second producer of it.

This is exactly the same edge `persist_scheduled_run_completion` already
uses to flip `scheduled_task_runs.status` (§2.2) — two independent
consumers of one authoritative signal, added at the same call site
(`helpers.py`, immediately after the existing `persist_live_status` /
`persist_scheduled_run_completion` calls), not two parallel definitions of
"is the session done."

### 5.2 `session.awaiting_decision` / `session.resumed` — do NOT bind to `"waiting"`

Both heads independently rejected binding these to the `"waiting"` status
literal, and this is a correction to an implicit assumption in the original
issue framing worth flagging back to the issue: `"waiting"` is a generic
status any `_publish_status` caller can set for reasons unrelated to a human
decision, while an elicitation is a structurally different event
(`response.elicitation_request`, observed through
`session_stream.publish()` → `pending_elicitations.record_publish()`,
`session_stream.py:131`) that can fire without an accompanying `"waiting"`
write, and vice versa. Binding to `"waiting"` both over-fires and
under-fires.

The correct chokepoint is `pending_elicitations.record_publish()` itself:

- `session.awaiting_decision` fires on **first insertion** of a given
  `elicitation_id` (`event_type == "response.elicitation_request"`) — one
  event per `elicitation_id`, not per session, since a session can have more
  than one outstanding decision and each needs to be individually
  addressable per the acceptance criterion. `record_publish()` does not
  currently expose a first-insert-vs-republish result; add one (`added` /
  `replaced_same_id` / `removed` / `not_found`) so the outbox hook can gate
  on `added` without re-deriving idempotency itself. The outbox's own
  unique `(session_id, event_type, transition_key)` constraint (§4.2) is a
  second, storage-level safety net if that in-memory gate is ever wrong.
- `session.resumed` fires on **successful resolution of that specific
  elicitation_id** — not on the pending-count reaching zero. A session can
  have multiple outstanding elicitations; resolving one may resume real
  work even while another remains outstanding, so gating on "count reaches
  zero" would under-report multi-prompt sessions.

### 5.3 Deterministic transition keys

```
turn:{response_id}:completed
turn:{response_id}:failed
elicitation:{elicitation_id}:awaiting_decision
elicitation:{elicitation_id}:resumed
```

Never synthesized from a process-local counter — always derived from an ID
that is already stable across the relay/hook boundary that produced it.

### 5.4 The one disagreement the debate did not resolve, and how it's resolved here

**Both heads' opening positions were the opposite of each other on this
point, and after one full cross-critique round they swapped — landing on
the opposite conclusion from where each started — rather than converging.**
That persistence, not the first-round positions, is the signal this needed
a resolution here rather than being left open:

- **Claude's final position**: build a durable `session_elicitations`
  table (verdict + context + status), because the acceptance criterion
  "decision response resumes the exact session" cannot be satisfied by an
  outbox event alone — the in-memory `Future` and its owning process are
  gone after a restart regardless of what the outbox does, so *something*
  durable has to record that a decision was made, with a reconnect
  reconciliation path that replays a `decided`-but-unresolved-Future row
  into the reconnected runner.
- **GPT's final position**: do NOT build that table in OMN-104. A SQL row
  recording a verdict cannot revive a killed subprocess-backed harness
  process either way (`HarnessProcessManager` is torn down in the same
  lifespan shutdown that would precede a restart) — the table would only be
  load-bearing for native/tmux-attached harnesses, which already have their
  own out-of-process resume machinery
  (`_harness_pre_resolved_elicitations`), making a general-purpose SQL
  ledger a **second, generic mechanism that can disagree with the
  harness-specific one that already exists** for the one case where
  durability is actually achievable. Scope this out; make the decision
  endpoint fail loud (409/410) for a no-longer-resolvable elicitation
  instead.

**Resolution adopted for this design: build the table, narrower than
Claude's original shape, with GPT's harness-classified failure mode as a
hard requirement rather than a fallback.** Reasoning:

1. The acceptance criteria in §1 are an *approved product decision*, not
   something this design gets to narrow unilaterally — "decision
   payload/context," "restart/network replay," and "end-to-end decision
   response resuming the exact session" are three separate bullets, and the
   first two are unconditionally achievable with a durable table regardless
   of harness class (the decision is recorded and survives a restart even
   when the third bullet can't be honored for a given harness).
2. GPT's objection is correct as a *scope* argument for full resume-across-
   restart, not as an argument against persisting the decision at all —
   persisting the verdict durably is what makes the loud-failure behavior
   GPT itself proposes possible to distinguish from a silently-dropped
   verdict (without the table, the decision endpoint has nothing to check
   against after a restart to know whether it should 409 or accept).
3. So: add `session_elicitations` (schema below), written transactionally
   alongside the `session.awaiting_decision` / `session.resumed` outbox
   rows. On the decision endpoint, **always** persist the verdict first.
   Then attempt reconnection, with the outcome *explicitly classified in
   both the API response and the test matrix (§12)* rather than uniformly
   promised.

   **The precise classification axis, sharpened during the debate's second
   round, is not "harness type" but "which process died."** There are two
   separate in-memory elicitation registries, not one, verified against the
   current tree: the **server-side** registry
   (`_harness_elicitation_registry: dict[str, asyncio.Future[ElicitationResult]]`,
   `_elicitation_registry.py:15`) and a **runner-side** registry
   (`_pending: dict[str, asyncio.Future[bool]]`,
   `omnigent/runner/pending_approvals.py:48`), resolved when the server
   POSTs an approval event to `/v1/sessions/{id}/events`. That split maps
   directly onto three distinct restart scenarios:
   - **No restart**: the server-side `Future` is still parked — resolve it
     immediately, same as today.
   - **Server restarts, runner/host process stays alive** (a routine
     rolling redeploy under the confirmed multi-replica topology — the
     dominant real-world restart case). The runner-side `Future` in
     `pending_approvals.py` is still alive and parked; only the *delivery*
     to it was lost when the server-side registry that would have sent it
     restarted. A durable `session_elicitations` row plus "on tunnel
     reconnect, re-POST any decided-but-undelivered row to the runner"
     **genuinely resolves this case** — it completes an existing pattern
     (the `_harness_pre_resolved_elicitations` tombstone already handles the
     analogous case of a dropped-and-reparked *hook* long-poll) rather than
     inventing a new one, and covers native/tmux-attached *and*
     subprocess-backed harnesses alike, since the runner process itself
     never died.
   - **The runner process itself dies** (host crash, subprocess killed).
     The runner-side `Future` and the coroutine stack awaiting it are
     unrecoverably gone — no durable row can rebind a coroutine that no
     longer exists, regardless of harness type. Return `410 Gone` with
     `error_code = "elicitation_not_resolvable"` here — the verdict is
     durably recorded (audit trail, and idempotent if the manager retries),
     but the endpoint does not claim the session resumed, because it
     structurally did not. Emit `session.resumed` only on the runner's
     acknowledgement of having consumed the decision, never on the manager
     HTTP call being accepted — that ack is what makes the event truthful.

   Implementation note: the reconnect-redelivery path in the middle case is
   genuinely new wiring (a tunnel-reconnect hook that queries
   `session_elicitations` for `decided`-but-undelivered rows and re-POSTs
   them), but it reuses the runner's own existing approval-POST contract
   rather than inventing a second delivery mechanism.

This makes the acceptance test for "restart before verdict" an explicitly
harness-classified test (§12), rather than either quietly failing to build
what was approved (GPT's original scope-narrowing) or promising a resume
guarantee the subprocess harness class cannot honor (Claude's original
general reconnect-reconciliation framing, before its own second pass
narrowed the same way).

```
session_elicitations   (OmnigentBase — same base as session_lifecycle_outbox)
  workspace_id       BigInteger        -- PK member
  id                 Uuid16            -- PK member; == elicitation_id
  session_id         Uuid16            -- indexed, no DB FK (Rule R032)
  status             SmallInteger      -- pending=1, decided=2, delivered_to_runner=3, expired=4 (new enum_codecs entry)
  request_payload    CompressedText    -- allowlisted per §3.2, written at registration
  decision_payload   CompressedText NULL
  decided_by         String(128) NULL  -- manager identity from the signed callback; NULL for web-UI verdicts
  created_at         Integer
  decided_at         Integer NULL
  resolved_at        Integer NULL      -- when a live/reconnected runner actually consumed the decision
```

Write path: the row is created (`status=pending`) at the same call site that
populates `_harness_elicitation_registry`
(`orchestration.py:416`) — before `_publish_status("waiting", ...)`, in the
same transaction as the `session.awaiting_decision` outbox insert. The
decision endpoint (extending the existing verdict-PATCH path) writes
`decision_payload` / `decided_at` / `status=decided` **before** attempting
to resolve any in-memory Future, and inserts the `session.resumed` outbox
row only once the decision is durably recorded — matching §5.1's rule that
`_publish_status`/outbox producers never depend on delivery succeeding.

---

## 6. Per-session ordering and non-regression

- `sequence` (§4.1/4.2) is allocated under the `session_lifecycle_cursors`
  lock in the same transaction as the event insert — monotonic per
  `(workspace_id, session_id)`, gap-free with respect to committed rows
  (a rolled-back producer transaction never consumes a sequence number that
  reaches the outbox).
- The dispatcher (§7) delivers **only the lowest non-delivered `sequence`
  for a given session** at a time — never issues session event N+1's HTTP
  call before N has reached a terminal delivery state (`delivered` or
  `dead_letter`, which still counts as "resolved for ordering purposes,"
  see §7). This is what prevents a slow/retrying early event from being
  overtaken by a fast later one at the dispatch layer.
- Ordering at the dispatch layer is necessary but not sufficient — network
  retries can still reorder in flight. The wire payload therefore always
  carries `sequence`, and the **documented consumer contract** (enforced by
  the manager, not by Omnigent) is: discard any incoming event whose
  `sequence` is ≤ the highest already applied for that `session_id`. This
  is the actual mechanism that satisfies "delayed events cannot regress
  manager state" — dispatch-side ordering reduces how often this path is
  exercised, it doesn't replace the need for it.

---

## 7. Dispatcher: ownership, concurrency, retry/backoff, dead-letter, shutdown

### 7.1 Ownership and lifecycle

Runs as a single `asyncio.create_task` inside the existing FastAPI lifespan
(`app.py:933`), the same pattern as `metrics_publish_task` (`app.py:1052`):
started after startup, `.cancel()` + `await` under
`suppress(asyncio.CancelledError)` on shutdown (`app.py:~1124`). One task
per server replica — safe under multi-replica because claiming is
row-leased (below), not because only one replica runs it.

### 7.2 Claim query: `SKIP LOCKED`, a new pattern, justified

`session_live_state.py`'s own rationale for existing at all is multi-replica
contention ("a request can land on any replica"). Under N dispatcher
replicas polling the same table, plain `FOR UPDATE` serializes claims —
replica B blocks behind replica A's lock instead of claiming a different
row, defeating the purpose of running N replicas. `FOR UPDATE SKIP LOCKED`
(PostgreSQL 9.5+) is the standard fix for exactly this worker-pool claim
shape, and is introduced here as a narrow, dispatcher-scoped exception to
the codebase's existing plain-`FOR UPDATE` convention — not a replacement
for it elsewhere. On SQLite (`_meta_supports_for_update`-equivalent false),
single-writer serialization is already free, so a plain
`WHERE status='pending' AND next_attempt_at <= now() LIMIT N` under
`BEGIN IMMEDIATE` is sufficient; no third locking convention is introduced.

Claim → lease (`lease_owner`, `lease_expires_at`) → commit → deliver
**outside** the transaction (never hold a DB lock across the outbound HTTP
call) → on 2xx, mark `delivered`; on failure, compute next backoff and
release the lease.

### 7.3 Backoff schedule

Fresh, small implementation (not a refactor of `llm_retry.py` or
`tool_retry.py` — both are already independent per-subsystem
implementations, so there is no single retry module this feature is
obligated to converge with; forcing a merge now would couple two unrelated
subsystems' retry semantics for no shared benefit). Exponential with full
jitter, capped:

```
delay = min(cap, base * 2^attempt) * random(0.5, 1.0)
base = 5s, cap = 15min, unbounded attempt count
```

### 7.4 Dead-letter: escalation, not abandonment

Both heads independently rejected copying `run_reconciler.py`'s lazy-on-read
model for this specific piece, for a reason worth stating precisely because
it's a deliberate divergence from an existing precedent, not a default:
`run_reconciler.py`'s own docstring justification is that reconciliation
happens "the moment [an orphan] would otherwise be shown" — the *read path
is itself the trigger*. A dead-lettered manager webhook has no equivalent
read-path trigger: the manager specifically asked for push so it would not
have to poll, so hanging recovery off "a user happens to view a session"
would turn a transient network outage into an indefinite deadlock for a
manager that is, by construction, not the one looking.

So: after a configurable attempt/age threshold, mark the row `dead_letter`
— **for API/log visibility and alerting only**. It keeps retrying at the
capped floor interval (§7.3's `cap`), never stops automatically. This
follows directly from the approved contract ("retry until 2xx") and from
ordering (§6): a permanently abandoned head event would either violate the
"retry until 2xx" contract outright, or silently block every later event
for that session forever, whichever way it's read. An operator can pause a
specific outbox row or the manager-webhook target entirely (§8 config); there
is no automatic discard.

A short periodic sweep (same lifespan-task pattern, not a separate
mechanism) reclaims rows whose lease expired without the claiming replica
completing delivery (crash mid-claim) — `WHERE status='leased' AND
lease_expires_at < now()`. This is the one place a sweep is added where
`run_reconciler.py` uses none, and the justification is the same asymmetry:
`run_reconciler.py` has a user-facing read endpoint to lazily piggyback on;
the dispatcher does not, so orphaned leases need their own reclaim path
rather than waiting on a read that may never come.

### 7.5 Never blocks terminal transitions

The outbox INSERT (§4.3) is synchronous with the producing transaction, but
the **HTTP delivery** is fully decoupled — the dispatcher is a separate
task, so a slow or unreachable manager endpoint never delays
`_publish_status`, the SSE stream, or any session terminal-state write. This
satisfies the "callback delivery never blocks or changes session terminal
transitions" requirement by construction: the delivery loop has no code
path that can write back into `_session_status_cache`,
`omnigent_conversation_metadata`, or any conversation table.

---

## 8. Endpoint/secret configuration, HMAC scheme

### 8.1 Configuration

Following the existing split (`server_config.py`): non-secret settings in
`<data_dir>/config.yaml` under a new `manager_webhook` block
(`enabled`, `endpoint`, `key_id`); the secret itself is env-var only,
`OMNIGENT_MANAGER_WEBHOOK_SECRET` (and an optional
`OMNIGENT_MANAGER_WEBHOOK_SECRET_PREVIOUS` for rotation — verify against
both during a rotation window, sign only with the current one). Require
HTTPS for the endpoint; reject `http://` at config-load time except for an
explicit local-dev override, mirroring how other security-relevant settings
in this codebase fail closed on misconfiguration rather than warn-and-continue.

### 8.2 HMAC canonicalization

No outbound-signing precedent exists in this codebase to extend (§2.5), so
this is a fresh design, following the widely-deployed webhook convention
(Stripe-style: sign the raw transmitted body plus a timestamp, verify
before JSON parsing) rather than inventing a bespoke scheme:

```
signed_content = f"{timestamp}.{event_id}.{raw_json_body}"
signature = hex(HMAC_SHA256(secret, signed_content))
```

Headers:

```
Content-Type: application/json
X-Omnigent-Event-Id: <event_id>
X-Omnigent-Event-Type: <event_type>
X-Omnigent-Delivery-Attempt: <1-based attempt count>
X-Omnigent-Timestamp: <unix seconds>
X-Omnigent-Key-Id: <key_id>              # supports secret rotation without a payload change
X-Omnigent-Signature: v1=<hex hmac>
```

Replay defense: the receiver is expected to reject a request whose
`X-Omnigent-Timestamp` is more than a configured tolerance (recommend 5
minutes) away from its own clock, in addition to verifying the signature —
documented in the manager-integration contract, enforced by the consumer,
not by Omnigent (Omnigent cannot force a third party's verification
behavior; it can only make it correct and simple to implement, matching how
`X-Omnigent-Timestamp` closes the same class of replay window Stripe's own
scheme does).

---

## 9. API and log observability

- `GET /v1/sessions/{session_id}/manager-webhook-deliveries` (new,
  authorized like other session sub-resources) — `event_id`, `event_type`,
  `sequence`, `status`, `attempt_count`, `last_attempt_at`,
  `last_http_status`, `last_error_code`/`last_error_message` (already
  sanitized at write time, §3.2). Modeled directly on the existing
  `GET /scheduled-tasks/{id}/runs` shape (`scheduled_tasks.py:380`).
- Session snapshot gains a `latest_manager_delivery` summary field
  (status + last attempt) so the sidebar/detail view doesn't need a second
  round-trip for the common case.
- Logs: one structured line per delivery attempt — `event_id`,
  `workspace_id`, `session_id`, `sequence`, `attempt_count`, `event_type`,
  outcome, latency, `last_http_status`, sanitized `error_code`. Never the
  payload body, the signature, the secret, the raw endpoint URL (log only
  its host, matching how other outbound-URL logging in this codebase avoids
  leaking full query strings), or any `Authorization`-class header value —
  reuses `_REDACT_KEY_SUBSTRINGS` (`telemetry.py:124`) as the log-line
  filter, distinct from and stricter than the payload allowlist in §3.2.

---

## 10. Callback / decision-response flow

Covered in full in §5.4; summarized here as a flow:

1. Elicitation raised → `session_elicitations` row (`pending`) +
   `session.awaiting_decision` outbox row, one transaction, at
   `orchestration.py:416`'s call site.
2. Manager receives the webhook (retried until 2xx per §7), extracts
   `elicitation_id` + `session_id` + allowlisted `request_payload` from
   `data`.
3. Manager calls the (existing, extended) decision endpoint with its
   verdict.
4. Endpoint persists `decision_payload`/`decided_at`/`status=decided`
   first, unconditionally.
5. Endpoint attempts resolution per harness class (§5.4): live Future →
   immediate; native/tmux → queued for next reconnect; subprocess with dead
   process → `410` with `elicitation_not_resolvable`, verdict still
   recorded.
6. On successful resolution (immediate or reconnect-replayed), insert
   `session.resumed` outbox row, `resolved_at` stamped.

---

## 11. Rollout, migration, backward compatibility, reconciliation

- Purely additive: two new tables (`session_lifecycle_outbox`,
  `session_lifecycle_cursors`) plus one extended table
  (`session_elicitations`), one new `enum_codecs` entry, one new config
  block, zero changes to any existing table's shape. No existing endpoint's
  response shape changes.
- `manager_webhook.enabled = false` by default; the dispatcher task itself
  starts but immediately no-ops (claims nothing) if disabled, rather than
  being conditionally constructed, so enabling it live via config reload
  needs no server restart.
- **Poll/reconcile stays enabled unconditionally, independent of this
  feature's health** — the acceptance requirement is explicit that push is
  additive to polling, never a replacement. Nothing in this design gates
  any existing reconciliation path on the outbox's state; a manager that
  never configures a webhook, or whose webhook is entirely broken, sees
  identical session-state correctness to today via polling — it only loses
  the low-latency wake-up.
- Migration is a single new-tables-only Alembic revision (matching
  `d7f1a2b3c4e5`'s shape) — no backfill needed, since the outbox has no
  history to reconstruct (events start accruing from the migration forward;
  a manager onboarding onto an existing deployment relies on poll/reconcile
  for pre-existing session state, exactly as today).

---

## 12. Test matrix (one row per acceptance bullet)

| Acceptance bullet | Test |
|---|---|
| Completion wake | Turn reaches `idle` from an open `response_id` → exactly one `session.completed` row, delivered, **including** when a background shell is still running (asserts the fix in §5.1: this must NOT be swallowed); a session that never opened a turn, or a same-status no-op republish → zero rows |
| Decision payload/context | `session.awaiting_decision` payload round-trips the allowlisted request fields; a field outside the allowlist (e.g. raw env dict on the harness side) is asserted absent from the payload |
| Restart/network replay | Kill dispatcher mid-delivery (lease held, no ack) → row remains `leased` until `lease_expires_at`, then is reclaimed and re-delivered with the same `event_id`/`sequence`; server process restart between outbox insert and first delivery attempt → row survives (durable), dispatcher picks it up on the next claim cycle |
| Duplicate delivery / stable IDs | Manager 2xxs but the ack is lost (simulated) → dispatcher retries, receiver-side dedupe on `event_id` proven via a test double manager that idempotency-checks |
| Signature/redaction security | Tampered body/timestamp fails verification; timestamp outside tolerance is rejected by the reference-verification test even with a valid signature; log lines for a delivery attempt assert absence of payload body, secret, signature, full URL |
| End-to-end decision response resuming the exact session, **classified by which process died (§5.4)** | (a) no restart: server-side Future still parked, verdict resolves immediately, `session.resumed` fires. (b) server restarts, runner/host process alive: verdict persists, redelivered to the runner's still-live `pending_approvals` Future on tunnel reconnect, `session.resumed` fires on runner ack — this is the dominant real-world case and must pass for BOTH native/tmux and subprocess-backed harnesses. (c) runner process itself dies: decision endpoint returns `410 elicitation_not_resolvable`; verdict is still durably recorded (assert via direct row read, not via the HTTP response); poll/reconcile subsequently reflects the session's real post-restart state |
| Ordering / non-regression | Two events for one session enqueued out of delivery order (simulated retry timing) → manager-side stable-ID + sequence dedupe test proves a lower `sequence` arriving after a higher one is a documented no-op for the consumer; dispatcher never issues session event N+1 before N reaches a terminal delivery state |
| Callback never blocks terminal transitions | Manager endpoint made to hang/timeout; assert `_publish_status`, the SSE stream, and `conversation_labels`/`live_status` writes complete with unchanged latency |
| Poll/reconcile stays enabled | With `manager_webhook.enabled=false` and with the manager endpoint permanently down, existing reconciliation-dependent behavior (session-state correctness) is unaffected |
| Dead-letter is escalation not abandonment | Force an event past the dead-letter threshold; assert it still appears in the next claim cycle at the capped floor interval, and that it is visible via the new API/log fields |

---

## 13. Implementation slices, likely files

1. **Schema**: new Alembic revision — `session_lifecycle_outbox`,
   `session_lifecycle_cursors`, `session_elicitations` (extends the
   existing in-memory-only elicitation concept with a durable row);
   `omnigent/db/enum_codecs.py` new entries for `event_type` and the two
   new `status` enums. Store methods in
   `omnigent/stores/conversation_store/sqlalchemy_store.py` (or a new
   sibling store, given these are `OmnigentBase` not conversation content —
   worth a short implementation-time decision, not a design blocker).
2. **Producer hooks**: `omnigent/server/session_outbox.py` (new module,
   parallel to `session_live_state.py`); call sites added in
   `omnigent/server/routes/_sessions/helpers.py` (`_publish_status`, near
   the existing `persist_live_status`/`persist_scheduled_run_completion`
   calls) and `omnigent/runtime/pending_elicitations.py`
   (`record_publish`'s first-insert/resolve edges) and
   `omnigent/server/routes/_sessions/orchestration.py:416`'s elicitation
   registration call site.
3. **Dispatcher**: new module (e.g.
   `omnigent/server/manager_webhook_dispatcher.py`), wired into
   `omnigent/server/app.py`'s lifespan (`app.py:933`/`1052`) alongside
   `metrics_publish_task`.
4. **HMAC/config**: `omnigent/server/server_config.py` (new
   `manager_webhook` block), a small signing helper module (no existing
   module to extend per §2.5/§8.2).
5. **Decision endpoint extension**: the existing verdict-PATCH path that
   resolves `_harness_elicitation_registry`
   (`omnigent/server/routes/_sessions/orchestration.py` area) — add the
   durable-persist-first step and the harness-classified resolution
   outcome (§5.4).
6. **API**: `GET /v1/sessions/{id}/manager-webhook-deliveries`, modeled on
   `omnigent/server/routes/scheduled_tasks.py:380`.
7. **Redaction**: extend/reuse `omnigent/runtime/telemetry.py`'s
   `_REDACT_KEY_SUBSTRINGS` for the log-line path; new explicit allowlist
   constant for the elicitation payload path (§3.2) — these are
   deliberately two different mechanisms, not one generalized redactor.

---

## 14. Rejected alternatives and non-goals

- **Deriving `session.completed`/`.failed` from `_session_status_cache`
  reads or from `live_status` polling** — rejected; both are documented
  best-effort display projections (§2.1), not an event log, and reading
  them would mean a dropped best-effort write silently also drops the
  manager-facing event with no retry path. The outbox must be written from
  the same authoritative edge, not from either mirror.
- **Merging the outbox write into `session_live_state.py`** — rejected
  (§4.3); opposite durability postures (best-effort vs. must-persist)
  belong in visibly separate modules, not one module with an internal fork
  a future editor can miss.
- **Binding `awaiting_decision`/`resumed` to the `"waiting"` status
  literal** — rejected (§5.2); demonstrably both over-fires and
  under-fires relative to the actual elicitation events.
- **Reusing `_lock_conversation` for outbox sequence allocation** —
  rejected (§4.1); it locks through the conversation engine, which is not
  guaranteed to be the same physical database as the `OmnigentBase` outbox
  table under a split-engine deployment.
- **Copying `run_reconciler.py`'s pure lazy-on-read backstop for
  dead-lettered events** — rejected (§7.4); that precedent's own
  justification (the read path is the reconciliation trigger) doesn't hold
  for a manager that is, by definition, not the one reading.
- **A general-purpose durable elicitation/decision table that promises
  cross-restart resume for every harness class uniformly** — rejected in
  favor of the harness-classified version (§5.4); the uniform promise is
  not honorable for subprocess-backed harnesses regardless of what SQL
  schema backs it, and claiming otherwise in the API/tests would be
  advertising a guarantee that silently fails for a whole harness class.
- **Plain `FOR UPDATE` for the dispatcher's claim query** — rejected
  (§7.2) as the default because it serializes claims across replicas,
  defeating multi-replica dispatch; kept only as the SQLite fallback where
  `SKIP LOCKED` isn't available and single-writer serialization is already
  the existing behavior.
- **True dead-letter abandonment (stop retrying entirely)** — rejected
  (§7.4); violates "retry until 2xx" and would permanently block
  per-session ordering for every event after the stuck one.
- **Non-goal**: building the "shared backplane" for
  `pending_elicitations`/`_harness_elicitation_registry` that their own
  docstrings already name as future work. OMN-104 adds a durable
  *elicitation ledger* narrowly scoped to what the manager-webhook contract
  requires (§5.4); it does not generalize the in-memory registries
  themselves into a cross-replica system.
- **Non-goal**: reviving a subprocess-backed harness's killed process
  across a restart. Out of reach for any design that doesn't also change
  process supervision/lifecycle, which is out of scope for this ticket.

---

## Handoff (for a separate Polly implementation session)

- Start from §13's slice list; slice 1 (schema) and slice 4 (HMAC/config)
  have no dependency on each other and can run in parallel; slices 2, 3, 5
  depend on slice 1's tables existing.
- The one place this design makes a judgment call beyond what the debate
  fully resolved is §5.4's harness-classification of decision-resume — flag
  this back to the issue/product owner for explicit sign-off before
  implementation starts, since it changes what "end-to-end decision
  response resuming the exact session" means for a subprocess-backed
  harness (durable record + loud 410, not a resume guarantee).
- `record_publish()`'s first-insert-vs-republish return value (§5.2) is a
  small, self-contained change to `omnigent/runtime/pending_elicitations.py`
  worth landing and testing independently before the outbox hook that
  depends on it.
- No product code was written or modified in this session; this document
  and its commit are the entire deliverable.
