# Slack integration — design & architecture

How the Omnigent Slack bot is built and the key technical decisions behind it.
For operator setup (scopes, `.env`, running the daemon) see `README.md`; this
doc is for people working on the code.

## What it is

A Slack **Socket Mode** bot that bridges Slack to a single, operator-configured
Omnigent server. It maps **one Slack thread ↔ one Omnigent session**, streams
the agent's answer into the thread live, and renders tool-approval /
`AskUserQuestion` prompts as interactive Block Kit cards.

The **guiding principle**: the Omnigent **web UI is the reference client** for
the server API. Where possible the bot mirrors how the web UI consumes the
server (server-authoritative state, push-driven streaming, no invented polling);
deviations exist only where Slack's transport genuinely differs from a browser
tab, and are called out below.

## Module layout

Responsibilities are split so no single file owns streaming + orchestration +
I/O at once (the web UI splits a pure reducer from its orchestration store; this
is the Python analogue).

| Module | Responsibility |
| --- | --- |
| `events.py` | Pure SSE parsing + event DTOs + extractors (`extract_delta`, `session_status`, `extract_elicitation_request`, …). No I/O, no state. |
| `omnigent.py` | HTTP/SSE client (`OmnigentClient`), connection pool, the `run_turn` stream loop and turn-end detection, error subclasses. |
| `streaming.py` | The streamed-answer state machine: `_LiveReply` (Slack `chat.*Stream` buffering/seal/reopen) and `_AnswerReply` (ack lifecycle, seal-⇒-forget, tail reconciliation). Home of the `SlackClientProtocol`/`SlackStreamProtocol` structural types. |
| `elicitation.py` | `ElicitationController` — in-turn approval/question orchestration (post card, spawn resolver, finalize on `elicitation_resolved`). |
| `approvals.py` | Elicitation vocabulary: `ElicitationCoordinator` (click↔resolver bridge), Block Kit card builders, `ElicitationOutcome`, click routing/parsing. |
| `notifications.py` | `SlackNotifier` — all outbound Slack messages (acks, replies, ephemerals, todo plan, deflection notices) + the text formatters. |
| `service.py` | `SlackOmnigentService` — event acceptance, turn routing, turn lifecycle. Delegates streaming to `streaming.py`, elicitation to `elicitation.py`, messages to `notifications.py`. |
| `setup.py` / `oauth.py` / `auth_manager.py` / `tokens.py` | Per-user setup modal, device/OIDC login flows, token storage (encrypted at rest). |
| `store.py` | SQLite: thread→session mapping and per-user config. |
| `app.py` | slack_bolt wiring: event handlers + the Block Kit action handlers. |

## The turn: streaming lifecycle

A turn is: user message → `POST /v1/sessions/{id}/events` → read the session
SSE stream → render events into the thread → detect turn end → stop reading.

### One stream per turn (a deliberate divergence from the web UI)

The web UI holds **one long-lived SSE stream per session** open for the whole
time the conversation is on screen; a turn boundary is just a reducer event. The
Slack bot instead opens **one stream per turn** (`OmnigentClient.run_turn`).

Why: Slack has no persistent per-thread viewer — events arrive as discrete
webhook callbacks, and a thread can sit idle for days. Holding an SSE stream
open per thread indefinitely isn't the web UI's situation. The cost of this
choice is that **turn-end detection becomes load-bearing** (the loop must decide
when to stop reading and free the thread) — see below.

### Turn-end detection is server-authoritative and harness-agnostic

This is the single most fought-over piece of the design; it went through several
wrong versions before landing here. The rule mirrors the web UI's reducer, keyed
on **"is a response currently open?"** — never on the harness name.

The server exposes `session.status` events, but `response_id` on them is
**harness-dependent by design** (documented in the server's `SessionStatusEvent`
schema): terminal-backed harnesses (claude-native, codex) stamp the turn's
`response_id` on their terminal `idle`/`failed` (the Stop-hook edge), while the
in-process runtime (claude-sdk / the `debby` orchestrator) emits **all**
`session.status` events id-less. There are also mid-answer *flaps*: claude-native's
PTY-activity watcher emits bare `idle` (no `response_id`) during sub-second
generation lulls — those are **not** turn ends.

The loop (`_run_turn_once`) therefore:

1. Marks a response **open** when it sees an id-bearing `running`/`waiting`.
2. **Ends** the turn on `idle`/`failed` when **(a)** it is id-bearing and matches
   the open response (or no id-bearing open was ever seen), **or** **(b)** it is
   id-less *and* no id-bearing response is open — the in-process harness, whose
   real end is an id-less `idle`.
3. **Ignores** an id-less `idle` while an id-bearing response is open (the
   claude-native mid-answer flap — ending here truncates the reply).
4. Never ends on `waiting` (both harnesses use it for "parked on sub-agents /
   async work").

Verified against both harnesses live. Explicit `response.failed`/`.cancelled`
and `turn.failed`/`.cancelled` are hard-terminals too.

### Dead-socket backstop

The stream never sends `[DONE]` and never closes on its own; the server sends
`session.heartbeat` roughly every 15s. So the **only** condition not signalled by
an event is a dead (half-open) socket. The loop treats "no event of any kind for
`idle_grace_seconds`" (default 600s — comfortably above the 15s heartbeat) as a
dead connection and ends. This is the one justified client-side heuristic: a dead
connection by definition can't send a signal.

Timing note: the read is bounded with `asyncio.wait` (not `wait_for`) — cancelling
the generator's `__anext__` would kill it — and the in-flight read is awaited in
`finally` before the stream context closes, or httpx raises "aclose(): async
generator already running".

### `_AnswerReply` / `_LiveReply` invariants (`streaming.py`)

- **Ack visibility, no gap.** A "_Working on it…_" placeholder is posted once the
  session is established (after a new thread's config-summary message, so the
  thread reads metadata → ack → answer) and removed **only once real content is
  on screen** — the first delta that actually flushes to Slack, or the finalizing
  `stop()` for a short buffered answer. Slack's SDK buffers appends
  (`buffer_size=256`) and flushes on threshold or stop; clearing the ack any
  earlier shows an empty thread. Because the ack follows session start, a failed
  start posts no placeholder to clear — just the error.
- **Ordering via seal.** A streamed reply is one Slack message anchored to its
  open-time timestamp, so text appended after a mid-turn out-of-band post (card,
  policy/file notice, first todo) would sort *above* it. Before every such post
  the reply is **sealed** (finalize the current segment; the next append opens a
  fresh message that sorts after the post) → true chronological order.
- **Flush-before-card.** Because the SDK buffers, short pre-interruption text
  would otherwise become visible only at the seal (coincident with the card).
  `flush()` forces the buffered text onto the screen *before* the card, matching
  the web UI's live reveal.
- **Tail reconciliation + no-delta fallback.** The final answer is whatever
  streamed; if the model committed a final item beyond the deltas, only the
  remainder is appended. If a turn streamed *nothing* (answer arrived committed-
  only), the newest server message is recovered as a last resort — guarded so it
  can't resurrect a prior turn's message (baseline compare) or re-post an answer
  an earlier sealed segment already showed (`already_delivered`).

## Elicitations (tool approvals & questions): pure-push

When a turn hits an approval-gated tool call or an `AskUserQuestion`, the server
emits `response.elicitation_request` and parks. The bot handles this **pure-push**,
mirroring the web UI: it **keeps reading the stream** and observes resolution as
a normal `response.elicitation_resolved` event — it does **not** block the read
loop or poll `pending_elicitations`.

Flow (`ElicitationController`):

1. On `elicitation_request`: seal the current reply, post the card, and spawn a
   background **resolver** task — then return so the loop keeps reading. (Verified
   from server source + live: an unresolved park does *not* emit an id-bearing
   terminal, so keeping the loop alive is safe — the turn-end detector won't fire
   during a park.)
2. The resolver awaits the Slack click via `ElicitationCoordinator` and POSTs the
   verdict; on timeout it declines so the server-side park releases.
3. On the pushed `elicitation_resolved` (our own verdict, or an answer in the web
   UI / another client): finalize the card in place, exactly once (`finalized`
   guard). If the answer came from elsewhere, the coordinator wakes the resolver
   with a `RESOLVED_EXTERNALLY` sentinel so it posts nothing.

Classification is by **decision shape, not the server's delivery mode**:

- **Binary approval** → Approve/Deny card, with a preview of the pending action.
- **`AskUserQuestion`** → radio buttons / checkboxes + Submit; selected labels go
  back as `content`. Option values carry the option **index** (labels can exceed
  Slack's 75-char value cap); the index is mapped back to the full label at
  resolve time.
- **Free-form typed input** (non-empty `requestedSchema`, no `ask_user_question`)
  → the bot can't collect it with buttons, so it posts a web-UI link and doesn't
  block. The turn stays alive and resumes once answered there.

The server defaults to `url`-mode elicitations, but the bot renders a url-mode
approval/question natively and resolves via the endpoint — only genuinely
uncollectable typed input falls back to the link.

## Concurrency: run-when-idle, two guards, no queue

There is **no client-side queue**. Whether a new owner message to an existing
thread runs is decided by two independent guards; both must pass:

1. **Local stream guard** (`_active_threads`, reserved synchronously before any
   await). One turn streams per thread at a time — a second concurrent stream
   would render the same events into Slack twice. A message arriving while the
   thread is streaming is deflected (not queued).
2. **Server-activity check** (`get_session_activity`, mirroring the web UI's
   send-gate `computeIsWorking` + pending-elicitation). Catches activity on the
   *session* the local guard can't see — e.g. a turn driven from the web UI. If
   the server reports `running`/`waiting` or a pending elicitation, the message
   is deflected with a notice (wait/interrupt, or answer the pending request),
   linking to the web UI.

If both pass (server idle, no local stream) the turn **runs** — Slack is a full
conversational surface, not kickoff-only. A message that races the check is safe
regardless: the server buffers a mid-turn submit and runs it as a continuation
(verified in server source; the web UI likewise queues client-side rather than
rejecting).

The local guard is safe from the stale-wedge that an earlier version hit, because
every turn is now bounded (turn-end detection + dead-socket backstop guarantee it
ends and releases).

## Authorization

Slack channels are multi-user, so the bot enforces a **per-thread owner** model
(the web UI, single-identity, needs none of this):

- A thread belongs to whoever started it. A follow-up from a different user isn't
  added to the session; they get a private "not your session" notice. The gate is
  **fail-closed**: an event with no user, or a record with no stored owner, is
  refused rather than run.
- Elicitation button/form clicks carry `"<owner> <session_id> <elicitation_id>"`
  in their control value. A click from anyone but the owner is rejected **before**
  any verdict is delivered — the card is visible channel-wide but only the owner
  can act.

## Turn-progress signals

Beyond the streamed answer, best-effort notices (never interrupt the stream):
`response.policy_denied` → "blocked by policy" notice; `response.output_file.done`
→ produced-file notice; `session.todos` → a plan message posted once then edited
in place.

## Errors

`_turn_error_text` is the single source of truth mapping known errors to
user-facing messages, shared by the session-startup and mid-turn paths:

- **401** → "log in again" (`/omnigent`).
- **Unreachable** → "reconfigure" (`/omnigent`).
- **No online host** → the `omni host --server …` command.
- **412 `harness_not_configured`** → the server's *curated* `error.message` (run
  `omnigent setup` on the host). Server error bodies are otherwise **not** echoed
  to the channel (they can leak internal paths/stack traces) — only this specific,
  actionable code's message is surfaced; everything else is logged server-side and
  shown as a generic failure.

## Authentication (per-user, delegated)

Each Slack user authenticates as their own Omnigent identity — no Omnigent
credential passes through Slack. The bot auto-detects the server's auth mode
(unauthenticated `GET /v1/me`) and drives `accounts`-mode device grant (RFC 8628)
or `oidc` cli-login inside the `/omnigent` modal; `header`/proxy mode is
unsupported. Tokens are encrypted at rest when
`OMNIGENT_SLACK_TOKEN_ENCRYPTION_KEY` is set, else in-memory only. The 401-retry
path refreshes a delegated token once mid-request. See `README.md#authentication`
for the operator/user view and `designs/DEVICE_AUTH.md` in the main repo for the
threat model.

## Testing notes

Unit tests use fakes (`FakeOmnigentClient`, `FakeSlackClient`) that mirror the
real SSE event shapes — including the id-bearing vs id-less `session.status`
distinction and the SDK's buffer/flush behavior — so the turn-end and streaming
invariants above are exercised without a live server. The trickiest behaviors
(turn-end per harness, silent-stream hang, pure-push elicitation, flush-before-
card) each have a regression test that fails without its fix. Live E2E against a
dev server is used for the timing-dependent flows fakes can't fully model.
