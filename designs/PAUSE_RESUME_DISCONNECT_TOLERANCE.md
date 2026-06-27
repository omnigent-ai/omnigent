# Disconnect-Tolerant Orchestration: Pause/Resume Across Client Disconnects

> **PROPOSAL.** Not yet implemented. This document captures the problem,
> root-cause analysis, target architecture, weighed options, and a staged
> build plan for making long-running multi-agent orchestration survive
> client disconnects.

## Problem

A long-lived orchestrator session (Omnigent "polly") fans work out to
sub-agent sessions (`claude_code`, `codex`, `pi`) that run autonomously to
completion and report back via an inbox. Sessions are runner-backed (a
server-side runner executes turns) and a human drives the orchestrator from a
client (laptop). A single orchestration run can span hours and dozens of
sub-agent turns.

When the client's network connection drops mid-run (laptop closed, commute,
wifi change), in-flight sub-agent turns **fail rather than pausing**. Observed
error signatures over one session, all correlated with the disconnect window:

| Signature | Where it surfaced |
|---|---|
| `native sub-agent turn failed / inner executor error: terminated` | all three vendors |
| `Claude native turn had no user text to send` | a resume/retry path |
| `no active turn context for tool dispatch` | the orchestrator's own tool calls |
| `Agent is already processing. Specify streamingBehavior ('steer' or 'followUp')` | a send racing an in-flight turn after reconnect |

These were initially misattributed to one vendor (`claude_code`) being
"unstable," but they hit **all three vendors plus the orchestrator's own tool
dispatch** and correlate with the disconnect window — i.e. it is a
transport/lifecycle issue, not a worker bug.

### Impact

- **Wasted work.** Turns that were progressing get killed; the orchestrator
  must detect the dark/failed result, re-verify git state, and re-dispatch.
- **Misdiagnosis.** Transient disconnect failures look like worker failures,
  leading to bad routing decisions (e.g. benching a healthy vendor).
- **Manual recovery.** The orchestrator had to inspect `git status` / branch
  tips to recover uncommitted work from cut-off sub-agents, then re-prompt
  them to commit/push/write the PR body.
- **No clean pause affordance.** There is no way to intentionally suspend a
  run before disconnecting and resume cleanly.

## Root cause

The unifying defect: **the client connection is load-bearing for server-side
execution.** A turn's lifecycle (and its in-memory context) is implicitly
scoped to the SSE/websocket stream, so when the socket dies, something
downstream interprets it as "abandon the work." There is **no
server-authoritative turn-state machine with an owner independent of the
client.** This single coupling explains every signature:

- **`inner executor error: terminated`** — the runner/turn is tied to the
  request context; tearing down the connection tears down the turn. This is
  why it hits *all* vendors and the orchestrator's own dispatch: the bug is in
  the shared transport/lifecycle layer, not any vendor adapter.
- **`had no user text to send`** — a retry/resume path fired with empty state
  because the in-memory turn buffer was lost when the connection dropped.
- **`no active turn context for tool dispatch`** — a tool call landed after
  its turn context was garbage-collected on disconnect; context was keyed to
  the live connection, not a durable turn record.
- **`Agent is already processing`** — the **reconnect race**: the server-side
  turn actually *survived* (or a duplicate started), and the client's
  post-reconnect send collided with it. The inconsistency — some turns die,
  some survive then reject the reattach — is itself a tell that turn ownership
  is ambiguous.

A secondary root cause: **completion/wake delivery is connection-coupled and
roughly at-most-once** — hence the duplicate/late wakes. The inbox is not a
durable, offset-based log the client replays from; it is a push that races the
socket.

## Goals — what "good" looks like

1. **Disconnect-tolerant turns.** A client disconnect must NOT terminate an
   in-flight sub-agent or orchestrator turn — the runner continues (or
   checkpoints) and lets the client reattach and pick up results.
2. **Idempotent reconnect.** Reconnecting reconciles state without "already
   processing" races; sends after reconnect queue/steer rather than error.
3. **Explicit pause/resume.** A way to quiesce a run (let current turns finish,
   hold new dispatches) and resume later, so the human can intentionally step
   away.
4. **Clear failure taxonomy.** Distinguish transport/disconnect failures from
   worker boot failures from worker task failures, so orchestration logic can
   retry-vs-rebench correctly.
5. **Auto-recovery of partial work.** A cut-off sub-agent's uncommitted
   worktree state is recoverable/resumable without hand-inspecting git.

## Target architecture

The architecture below is the convergent design from a structured debate
between two independent analyses (referred to as "A" and "B" where they
differ). They agree on the end state; their differences are noted as weighed
options.

### 1. Server-authoritative turn state (foundation)

Make a **Turn** a first-class durable entity owned by the runner, not the
client, with a persisted state machine:

```
CREATED → QUEUED → RUNNING → (COMPLETED | FAILED | CANCELLED)
                       ↘ PAUSING → PAUSED  (cooperative, resumable)
```

Invariants:

- The turn record lives in durable storage, keyed by `(session_id, turn_id)`,
  with `vendor`, `input_json`, `status`, `start_ts`, `end_ts`, `error_code`,
  `lease_owner`, `last_heartbeat_at`, `checkpoint_id`.
- Execution is owned by a **runner-side supervisor**, not the HTTP handler.
  The handler *observes* a turn; it never *owns* it.
- Client disconnect is a **non-event** for execution. It at most flips
  `attached: bool` / updates `last_client_seen`. It must **never** transition
  a `RUNNING` turn.

This single change ("disconnect cannot transition a turn") fixes the
`terminated` and `no active turn context` signatures directly.

### 2. Decouple execution from client liveness

- The **runner holds a lease** on the turn, not the client. Heartbeat is
  runner↔store on a runner-controlled timer. Lease expiry (runner crash) is
  the *only* thing that should orphan a turn, and it triggers a defined
  recovery path, not a silent kill.
- **Critical footgun:** the heartbeat must run on a thread/task **independent
  of turn execution**. If it shares the execution path, a busy/blocking tool
  call starves the heartbeat → false `LeaseExpired` → the turn is re-orphaned,
  rebuilding the original bug.
- The **client subscribes to a turn's event stream; it does not drive it.**
  Dispatch returns a `turn_id` immediately and is durable before any work
  starts. The socket becomes a *cursor* over a log, nothing more.

### 3. Reconnect & idempotency = reconcile, not resend

- Every mutating send carries a client-generated **idempotency key**
  (UUIDv7). A retried send with the same key is a no-op that returns the
  existing `turn_id`. This kills the empty-turn / duplicate-turn class.
- Replace the raw "already processing" error with a structured
  **`409 Attach-Required`** carrying `attach_url` + current `turn_id`, plus an
  explicit send intent chosen up front:
  - `enqueue` / `followUp` — queue behind the current turn; runs when it
    quiesces.
  - `steer` — inject into the in-flight turn (best-effort, per vendor
    capability).
  - `attach` / `follow` — observe the running turn from a given offset.
- **Defaults are caller-dependent (per-session policy):** orchestrator
  programmatic dispatch defaults to `enqueue`; an interactive human client
  reattaching defaults to `attach`/observe.
- Attach/replay endpoints require a **scoped, expiring token** bound to
  `{session_id, turn_id}` — a replayable transcript is a new read-side attack
  surface and must be authz'd.

### 4. Durable inbox / wake delivery (outbox pattern)

Separate two durability concerns that are easy to blur:

- The **streaming event log** (token/state replay) — see §7, deferrable.
- A durable **InboxMessage outbox** with a unique `notification_id` for
  completion/wake delivery — **not deferrable.**

On a turn milestone (`turn_started`, `turn_completed`, tool-result at a
boundary), enqueue a durable notification. On reconnect the client consumes and
ACKs from the inbox (`GET /inbox?since=…` → ACK). Belt-and-suspenders (stream
replay *plus* inbox dedup by `notification_id`) is the correct answer to the
duplicate/late-wake symptom, and the outbox is cheap and targeted enough to
ship early.

### 5. Failure taxonomy

Typed codes stored on the turn record and surfaced in metrics:

| Class | Meaning | Correct response | Feeds vendor health? |
|---|---|---|---|
| `TRANSPORT_DISCONNECT` | client socket dropped | none — keep running, let client reattach | **No** |
| `RUNNER_LOST` | lease/heartbeat expired, runner crashed | resume from last checkpoint | No (infra) |
| `WORKER_BOOT_FAILURE` | sub-agent failed to start (image/auth/quota) | retry w/ backoff; maybe reroute | Maybe (if persistent) |
| `WORKER_TASK_FAILURE` | agent ran but errored on the task | surface; route/retry per policy | **Yes** — real signal |
| `CANCELLED` | explicit pause-hard / user abort | expected, no penalty | No |

**Inviolable rule: vendor health scoring may only consume `WORKER_*`
classes.** Transport and runner-loss failures must be invisible to routing.
The original incident was a category error — transport noise polluting the
health signal. Only the runner knows the *source* of a termination
("worker-said-so" vs "my-connection-went-away"); the client must never label a
failure.

### 6. Pause/resume control surface

Distinguish two things people conflate:

- **Detach/attach** (connection-level, automatic): laptop closes → client
  detaches; work continues; laptop opens → client reattaches and replays. This
  should be *invisible and automatic*, falling out of §2–§3. No user gesture.
- **Pause/resume** (run-level, intentional): a control on the orchestrator
  session.
  - `pause(session)` → run mode `QUIESCING`: let in-flight turns **finish**
    (do not kill — that's the point), **hold new dispatches** in the queue,
    transition to `PAUSED` once no turn is `RUNNING`. Implemented as a **gate
    on the dispatch path**, touching nothing that is running.
  - `resume(session)` → `ACTIVE`, drain the held queue.
  - **Fan-out cascade:** `pause(polly)` blocks new sub-agent dispatch but lets
    already-dispatched sub-agent turns run to completion and report — otherwise
    you recreate the wasted-work problem.
- **Turn-level cooperative pause** (best-effort, capability-gated): a runner
  honors a pause-at-boundary signal at the **next tool-call (message)
  boundary**, persists a `checkpoint_id`, and sets `status = PAUSED`. This
  *reuses the checkpointing machinery* in §8 — it is not a separate subsystem.
  Vendors without control treat it as a no-op.

### 7. Streaming event log (the deferrable backbone)

Append-only, monotonic offsets per session: `turn_started`, `tool_call`,
`tool_result`, `token_delta`, `turn_completed`, `inbox_delivered`. Clients
subscribe with `last_seq` and the server replays from `last_seq + 1` then
continues live. Subscribers track `acked_seq`; GC events beyond a retention
window once all subscribers advance (or by TTL). Large artifacts referenced by
URL, not inlined. This unifies streaming and inbox and enables a clean live
UX — but it is a larger investment and can come after the inbox outbox.

### 8. Partial-work recovery

- **Workspace identity is durable and bound to the turn, not the connection.**
  A sub-agent's worktree/branch is named deterministically
  (`agent/<session_id>/<turn_id>`) and recorded in the turn record at dispatch
  — recovery never has to *discover* the branch.
- **Auto-checkpoint at tool-call (message) boundaries** — the only meaningful,
  serializable checkpoint (you cannot resume an LLM mid-generation across most
  vendor APIs). Use a scratch ref / `git stash create` and store the stash
  object; **never push WIP to the remote**. Record a `checkpoint` event with
  the commit SHA. Persist a manifest (files touched, `tool_call_id`s, observed
  side effects like `npm install x@1.2`) to object storage.
- **Tool-call idempotency + side-effect ledger.** Each tool call carries a
  `call_id`; adapters dedupe on it so retries/recovery are safe. This matters
  as much as top-level send idempotency for multi-step code agents.
- **On completion** the agent emits a structured result (branch, head SHA,
  dirty/clean, PR URL) into the inbox — "did it commit/push/PR?" becomes data,
  not forensics.
- **On `RUNNER_LOST`: resume from the last checkpoint — never live-reassign**
  an in-flight LLM turn to another runner.
- Because turns no longer die on disconnect (§1), the *common* path stops
  producing orphaned worktrees at all; checkpointing is the backstop for the
  genuine `RUNNER_LOST` case.

## Options weighed

### A. How disconnect-tolerance is achieved

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Keep turn tied to connection, add client auto-reconnect only** | smallest client change | server still kills work on drop; doesn't fix root cause; races persist | ✗ Rejected |
| **Server-authoritative turn state + runner lease** (chosen) | fixes root cause; turns survive drops; enables correct taxonomy & recovery | requires durable turn store + supervisor refactor | ✓ Chosen |

### B. Wake/completion delivery

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Connection push only (status quo)** | trivial | missed/dup wakes on disconnect | ✗ Rejected |
| **Full streaming event log first** | one unified mechanism; perfect replay | larger build; over-scoped for stopping the bleeding | Deferred (§7) |
| **Minimal durable inbox outbox now, event log later** (chosen) | cheap; kills missed/dup wakes immediately; clean migration path to the log | temporary dual mechanism until log lands | ✓ Chosen (both partners explicitly converged here) |

### C. Reconnect race ("already processing")

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Error and let caller retry (status quo)** | none | the bug; surfaces as failure | ✗ Rejected |
| **Idempotency key + structured `409 Attach-Required` + explicit intent** (chosen) | race impossible-by-construction on the normal path; caller-controlled semantics | needs client-SDK support + per-session default policy | ✓ Chosen |

### D. Pause granularity

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Dispatch-gate quiesce only** | universal (works for every vendor); trivial; low risk | time-to-`PAUSED` bounded only by the longest running turn | ✓ Ship first |
| **Turn-level cooperative pause at tool boundary** | bounds time-to-`PAUSED`; *free* once checkpointing exists | best-effort; vendor-capability-gated | ✓ Ship with checkpointing (§8) |
| **Mid-token / model-level pause** | finest control | a mirage — most vendor APIs cannot resume mid-generation | ✗ Rejected |

### E. `RUNNER_LOST` recovery

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Live-reassign the in-flight turn to a new runner** | no lost progress in theory | cannot reassign an in-flight LLM turn; risks double side effects on non-idempotent vendor jobs | ✗ Rejected |
| **Resume from last tool-boundary checkpoint** (chosen) | safe; deterministic; reuses checkpoint machinery | loses at most one incomplete step | ✓ Chosen |

> **Cross-cutting safety rule:** never auto-retry a non-idempotent,
> non-queryable vendor turn. Resume from checkpoint, or surface for human
> decision.

## Recommended build order

Sequenced by leverage. Phases 0–2 stop the bleeding; later phases are
investments.

- **Phase 0 — Hotfix (days).** Unlink turn cancellation from socket close — a
  dropped client must never flip a turn's cancellation token. In the client
  lib, auto-attach on "already processing" instead of erroring.
- **Phase 1 — Stop-the-bleeding (1–2 sprints), one release.**
  1. Server-authoritative `Turn` state machine owned by a runner-side
     supervisor under a lease + heartbeat (heartbeat on an **independent
     thread**).
  2. Idempotency keys on sends + structured `409 Attach-Required` with
     per-session default intent.
  3. Failure taxonomy + the `WORKER_*`-only routing rule.
- **Phase 2 — Minimal durable inbox outbox (1 sprint).** `notification_id`-keyed,
  acked delivery for turn milestones. Closes the missed/dup-wake gap cheaply.
- **Phase 3 — Dispatch-gate pause/resume.** Quiesce at session level; cascade
  across fan-out (block new sub-agent dispatch, let running ones finish).
- **Phase 4 — Tool-boundary checkpointing.** Deterministic worktree
  `agent/<session>/<turn>`, scratch-ref snapshots (never push WIP), structured
  completion result, tool-call side-effect ledger. Yields **both** auto
  partial-work recovery **and** best-effort turn-level pause as a single work
  item.
- **Phase 5 — Full append-only event log + `attach(last_seq)`.** The live-UX
  investment; unifies streaming + inbox and lets the separate inbox be phased
  out once clients migrate.

### How the plan resolves each symptom

| Symptom | Fixed by |
|---|---|
| `terminated` / `no active turn context` | Phase 1 (decouple + durable turn state) |
| `had no user text to send` | Phase 1 (idempotent send returns original input/turn) |
| `already processing / streamingBehavior` | Phase 0 + Phase 1 (intent + `Attach-Required`) |
| duplicate / late wakes | Phase 2 (durable inbox); fully correct in Phase 5 (log replay) |
| manual WIP salvage | Phase 4 (checkpoints + deterministic branch identity) |

## Tradeoffs and watch-outs

- **Durability cost / latency.** Writing turn + event records synchronously
  before acking adds latency. Negligible for hours-long runs; even
  append-to-disk + periodic fsync beats in-memory. Don't over-optimize.
- **At-least-once, not exactly-once.** Realistically you get at-least-once with
  idempotent effects via keys + checkpointing. Some vendors lack idempotency —
  avoid reissuing opaque vendor jobs unless you can query job status.
- **Storage growth.** Event log + artifacts need a retention window and
  out-of-band artifact storage; GC by seq watermark.
- **Heartbeat starvation.** (Repeated because it's the most likely
  self-inflicted regression.) Heartbeat must not run on the turn's execution
  path.
- **Security.** Attach/replay tokens must be scoped and expiring; don't let
  arbitrary clients attach to live runs or replay another session's transcript.
- **WIP history pollution.** Auto-checkpoints should use scratch refs /
  stash objects and be squashed/cleaned on real completion; never pushed.

## Open questions

- Per-session default intent policy: where is it configured, and what is the
  default for a *new* orchestrator session vs. an interactive human session?
- Should turn-level pause-at-boundary signals be wired into runner adapters
  from Phase 1 as no-ops (cheap hook, "design now build later") or only in
  Phase 4? (Minor; the one residual difference between the two analyses.)
- Lease TTL and heartbeat interval defaults, and the orphan-detection delay
  budget.
- Retention window for the event log and inbox before GC.
