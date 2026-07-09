# Queue + steer design

Client-side message queue with edit / delete / steer / reorder, for both SDK and
native harnesses.

## 1. Motivation

Today every message is **POSTed the moment the user hits send** — including
follow-ups typed while the agent is still working — and rendered immediately as an
optimistic bubble. The runner buffers a mid-turn message behind the active turn
and delivers it later, but the UI has already committed it. Problems:

- **No edit / delete / reorder.** Once POSTed the message is server-owned, so the
  user can't take back or fix a follow-up they queued in a hurry.
- **No queued-vs-sent visibility.** A follow-up sent mid-turn looks identical to a
  normal send — the user can't tell it's waiting behind the active turn, or when
  it will be picked up.
- **Silent cross-harness inconsistency.** The *same* action — "send a follow-up
  while the agent is working" — behaves differently per harness (mid-turn steer
  for live-queue SDKs, next-turn for everyone else) with no signal telling the
  user which they'll get.

The redesign fixes all three by holding the message in a **client-side queue
before it is POSTed**: the user can edit / delete / reorder while it waits, sees
it explicitly as "queued", and controls when it's sent (auto-flush on idle, or
steer now).

## 2. Proposal

Move the queue **client-side**. The strip becomes a pre-POST draft buffer; a
message is only sent to the server when it's flushed or steered.

```
   type → client queue "⏱ Queued" (NOT posted) → flush/steer → POST → bubble
   (strip = "not yet sent, still editable"; bubble = "sent, in flight")
```

### Queue behavior

- **Show as queued** when the agent is **not idle** (`sessionStatus` busy) — same
  signal for SDK and native.
- **Auto-flush head on idle (FIFO):** when the agent goes idle, send the head of
  the queue as the next turn. Type-ahead "just works" without any click.
- Persist the queue in `localStorage` (keyed by session) so it survives a hard
  refresh. (Trade-off: no cross-device sync — acceptable for unsent drafts.)

### Per-message actions

| Action | Behavior |
|--------|----------|
| **Edit** | pull the message back into the composer, purely client-side; persists across navigation/refresh |
| **Delete** | drop the message from the queue |
| **Steer** | POST it now (jump the queue) — deliver mid-turn where the harness supports it |
| **Reorder** | client-side drag (grip handle) to reorder the queue within a conversation |

### Promote-to-bubble rule

Promote a message from the strip into a normal chat bubble **as soon as it is
POSTed** (on flush or steer) — *not* when the agent consumes it. Once it's sent
there's no longer anything to edit / delete / steer / reorder, so the strip has
no reason to hold it.

The gap between (a) sent to server and (b) consumed by the agent becomes an
**implementation detail** the user need not see — because the strip no longer
represents server state, only the still-editable client buffer. This removes the
consume-timing dependency entirely.

### What "steer" means per harness

Steer always POSTs immediately; how it lands depends on the harness:

Steer always POSTs immediately (client-side, no runner change); how it lands
depends on the harness. The steer button is shown for **all** native sessions —
the runner delivers uniformly (POST → buffer → drain → hand to app, all natives'
`run_turn` return right after delivery), and the app decides what to do with a
message that arrives mid-response:

| Harness | Steer delivery | Mid-turn? |
|---------|----------------|-----------|
| claude-sdk / codex-sdk / pi-sdk | runner **live injection** (`_live_response_id` gate) | ✅ deterministic |
| cursor-sdk / copilot-sdk | buffer & drain | ❌ next turn |
| **codex-native** | explicit **`turn/steer`** RPC when a turn is active | ✅ deterministic *(verified)* |
| **claude-native** | `send-keys` into the **live pane**; the TUI folds the paste into the response | ✅ verified (best-effort timing) |
| cursor-native / hermes-native | `send-keys` paste into the **live pane** (`supports_enqueue=True`) | ⚠️ app-defined — mechanism confirmed in code, **not yet verified live** |
| pi-native | queued to the **resident extension** (`supports_enqueue=True`) | ⚠️ app-defined — mechanism confirmed in code, not yet verified live |
| opencode-native | HTTP prompt (`supports_enqueue=True`); the native server has **no live-steer endpoint** → admitted as a new prompt, promoted by the server's own queue at turn end | ❌ next turn (code-confirmed) |
| qwen / goose / kimi / kiro / antigravity -native | paste / file / RPC into the app (`supports_enqueue=True`) | ⚠️ app-defined — not yet verified live |

> **TODO (live verification):** every native harness above reports
> `supports_live_message_queue = True` and its delivery mechanism is confirmed
> in code (see the enqueue path per harness), but whether the vendor app folds
> the steered message in **mid-response** vs. at the **next turn** is confirmed
> against a *live* runner only for claude-native + codex-native. Run a live
> steer per harness to upgrade the ⚠️ rows. opencode-native is settled: its app
> server exposes no live-steer endpoint, so the steered message is always
> promoted at the next turn boundary.

**No runner change is required for native steer** — every native `run_turn`
returns right after delivering the input (decoupled from the response), so the
drain fires the next message quickly and it reaches the app while the prior
response is likely still running; the app does its own steering. Frame the UX
honestly: *"send now; the agent folds it into current work if it can"* — which is
exactly how native type-ahead already feels. Do **not** promise deterministic
mid-turn for the unverified natives.

**Steer is not interrupt.** In every case above, steer *does not cancel* the
running turn — the message is folded in at the agent's next natural breakpoint
(after the current tool/step completes), the same feel as steering native Claude
by typing while it works. For SDK, `enqueue_session_message` adds the message to
the running session's queue; the SDK surfaces it at its next turn-boundary — no
teardown. This is distinct from the **Interrupt** button, which really does
cancel the turn (`turn.cancel()`).

### Edges to handle

| Edge | Rule |
|------|------|
| POST fails after promote | revert the bubble to the queue (or error-badge it) |
| Agent goes idle mid-edit | editing pins the message out of auto-flush until re-committed |
| Native mirror-back | consume/mirror still needed as a **reconcile** signal (id-match the optimistic bubble to the real transcript item) so native round-trips don't double-render |

## 3. Appendix — lifecycle & topology

### Component topology

```
┌──────────┐  HTTPS+SSE  ┌──────────────┐  HTTP  ┌──────────┐  HTTP/UNIX socket  ┌─────────────────┐
│  CLIENT  │◄───────────►│  AP SERVER   │◄──────►│  RUNNER  │◄──────────────────►│ HARNESS SUBPROC │
│ (browser)│             │ persist+relay│        │ buffer + │  (1 per conv)      │ EXECUTOR=agent  │
└──────────┘             └──────────────┘        │ schedule │                    │ SDK: in-process │
                                                 └──────────┘                    │ native: →app ───┼─► tmux / RPC
                                                                                 └─────────────────┘
```

The agent runs **inside the harness subprocess** (SDK loop) or is **bridged out**
of it to a real app (native). It does **not** live in the runner process.

### Busy/idle signal (drives the queue)

| Harness | "running" from | "idle" from |
|---------|----------------|-------------|
| SDK | `response.created` → `_live_response_id` set | `response.completed` / stream-end |
| native | `UserPromptSubmit` hook | `Stop` / `StopFailure` hook (relayed by the transcript forwarder) |

Both surface to the client as the same `sessionStatus` field, seeded from the
snapshot on bind (correct after refresh, across tabs).

### Live-injection gate (SDK steer)

```python
_can_forward = (
    not _native                             # native uses paste / turn-steer, not this path
    and not _awaiting_approval              # don't steer a turn parked on a human gate
    and conversation_id in _live_response_id  # a response is actually streaming
)
```

### Native decoupling (why paste-steer works)

Native `run_turn` returns as soon as `send-keys` finishes pasting (not when the
agent finishes). `_active_turns` clears immediately, so the buffer drains the
next message quickly and it pastes into the still-live pane — the native app then
decides to steer it. `_native_pane_status` is the reliable liveness signal for a
long autonomous native turn (since `_active_turns` clears early).
