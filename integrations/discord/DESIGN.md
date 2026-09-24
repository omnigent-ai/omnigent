# Discord integration — design & architecture

How the Omnigent Discord bot is built and the key technical decisions behind it.
For operator setup (the app, intents, `.env`, running the daemon) see
`README.md`; this doc is for people working on the code.

## What it is

A Discord **gateway** bot that bridges Discord to a single, operator-configured
Omnigent server. It maps **one Discord conversation ↔ one Omnigent session**,
streams the agent's answer into that conversation live, and renders
tool-approval / `AskUserQuestion` prompts as embeds with buttons and select
menus.

The **guiding principle**, inherited from the Slack integration: the Omnigent
**web UI is the reference client** for the server API. Where possible the bot
mirrors how the web UI consumes the server (server-authoritative state,
push-driven streaming, no invented polling); deviations exist only where
Discord's transport genuinely differs, and are called out below.

## Relationship to the Slack integration

`omnigent-discord` is a **sibling package** of `omnigent-slack`, not a layer on
top of it. Both are standalone distributions that never import `omnigent` core:
a bot drives the server over HTTP, so it needs the API contract, not the server
implementation.

What the two genuinely share — SSE parsing, the HTTP/SSE client and its turn
loop, and the login flows — lives in **`omnigent-bot-core`**, which both depend
on. Nothing in it knows what a Slack thread or a Discord channel is. It is a
separate distribution from `omnigent-client` because that SDK depends on
`omnigent` core, which would drag the whole server package into both bots.

Everything that touches the chat surface — routing, streaming, cards, setup — is
written for Discord, because the two platforms differ in ways that reach the
design:

| | Slack | Discord |
| --- | --- | --- |
| Streaming | `chat.*Stream` API; Slack owns buffering and chunking | **No streaming API** — one message edited in place on a cadence, rolling into a continuation at 2000 chars |
| Threads | Every message has a `thread_ts`; DMs thread too | Real threads in guilds; **DMs have none at all** |
| Private notice | `chat.postEphemeral` anywhere | Ephemeral only in an **interaction response** — otherwise a DM, or a self-deleting channel message |
| Identity | User id is per-workspace, so keys are `(team, user)` | Snowflakes are **global**, so a bare user id is the key |
| Forms | Modals hold selects; option value cap 75 chars | ≤5 action rows, ≤25 options, value cap 100; a modal opens only from an interaction |
| Emoji | `:shortcode:` renders | Shortcodes are sent verbatim — **Unicode only** |
| Mentions | `@channel` in bot text is inert | Discord parses mentions out of raw content, so agent output could ping a server — the client denies all mentions by default |
| Slash commands | available everywhere once installed | A **guild**-scoped command is invisible in DMs; a **global** one is required, but in testing still did not surface in a bot DM |
| Install | A workspace admin installs the app | Anyone with Manage Server can add the bot — hence a **guild allow-list** |

## Module layout

Responsibilities are split so no single file owns streaming + orchestration +
I/O at once.

| Module | Responsibility |
| --- | --- |
| `streaming.py` | The streamed-answer state machine: `_LiveReply` (edit cadence, rollover, seal) and `_AnswerReply` (placeholder lifecycle, seal-⇒-forget, tail reconciliation). Home of the `MessageableProtocol`/`MessageProtocol` structural types. |
| `approvals.py` | Elicitation vocabulary, all pure: `ElicitationCoordinator`, `Card` builders, `ElicitationOutcome`, renderability rules, answer mapping. |
| `views.py` | The only `discord.ui` code for cards — buttons/selects, the owner check, and `Card` → `discord.Embed`. |
| `elicitation.py` | `ElicitationController` — in-turn approval/question orchestration (post card, spawn resolver, finalize on `elicitation_resolved`). |
| `notifications.py` | `DiscordNotifier` — all outbound messages (replies, private notices, todo plan, deflection notices) + the text formatters. |
| `service.py` | `DiscordOmnigentService` — event acceptance, turn routing, turn lifecycle. |
| `setup.py` | The `/omnigent` flow: pure card/option builders plus the selects and workspace modal. |
| `auth_manager.py` / `tokens.py` | Login orchestration and token storage (encrypted at rest). |
| `store.py` | SQLite: channel→session mapping, per-user config, message de-duplication. |
| `app.py` | discord.py wiring: intents, the client, the `/omnigent` command tree. |

`app.py`, `views.py`, and `setup.py` are the only modules that import `discord`;
everything else works against small structural protocols, which is what lets the
suite drive real code paths with recording fakes.

## Conversation model

A session is keyed on a **channel id**, which is globally unique in Discord — so
unlike Slack there is no workspace to prefix it with.

- **Guild channel.** The bot joins only when @-mentioned. It then **creates a
  thread on that message** and runs the session there. A streaming answer that
  is edited dozens of times would otherwise dominate a shared channel; the
  thread also gives the session a name, a member list, and an archive.
- **Inside a session thread.** The owner's plain messages continue the session —
  **no mention needed**. This is a deliberate divergence from Slack (where even a
  threaded reply must @-mention): the thread exists *for* the session, so
  demanding a mention every time is noise. A bystander's chatter is ignored
  silently; a bystander who @-mentions the bot gets a private explanation,
  because they addressed it directly and deserve an answer.
- **DM.** Every message counts. Discord DMs have no threads, so the DM channel
  itself is the session — and **`/omnigent new`** exists precisely to end one and
  start another, filling the role "open a new thread" plays in Slack.

### Ownership

Discord channels are multi-user, so the bot enforces a **per-conversation owner**
(the web UI, single-identity, needs none of this):

- A conversation belongs to whoever started it. A follow-up from anyone else is
  not added to the session. The gate is **fail-closed**: a record with no stored
  owner is refused rather than run.
- If the session record is gone a thread is still not adoptable: the bot reads
  the thread's **`owner_id`** off the gateway payload (no API call, so no
  failure mode) and requires it to be the requester, falling back to the starter
  message and **refusing when it cannot tell**. Every uncertain path answers no:
  refusing costs a new thread, granting hands away someone else's.
- Elicitation components carry the owner id on the view and check it in
  `interaction_check`, so a click from anyone else is refused **before** any
  verdict is delivered — the card is visible channel-wide but only the owner can
  act.

Guild membership is bounded separately: `OMNIGENT_DISCORD_GUILD_IDS` pins the bot
to the guilds the operator approved. Discord has no admin-install ceremony —
anyone with Manage Server can add a bot from an invite link — so without this an
unapproved community could reach the operator's Omnigent server (each user still
has to authenticate, but the bot should not be answering there at all).

## The turn: streaming lifecycle

A turn is: user message → `POST /v1/sessions/{id}/events` → read the session SSE
stream → render events into the conversation → detect turn end → stop reading.

### One stream per turn

The web UI holds one long-lived SSE stream per session for as long as the
conversation is on screen. The bot instead opens **one stream per turn**
(`OmnigentClient.run_turn`): Discord has no persistent per-channel viewer, and a
channel can sit idle for days. The cost is that **turn-end detection becomes
load-bearing**.

### Turn-end detection is server-authoritative and harness-agnostic

Unchanged from the Slack design, and the single most fought-over piece of it. The
rule mirrors the web UI's reducer, keyed on **"is a response currently open?"** —
never on the harness name.

`session.status` carries a `response_id` only for terminal-backed harnesses
(claude-native, codex); the in-process runtime emits every `session.status`
id-less. There are also mid-answer *flaps*: claude-native's PTY-activity watcher
emits a bare `idle` during sub-second generation lulls.

The loop (`_run_turn_once`) therefore:

1. Marks a response **open** on an id-bearing `running`/`waiting`.
2. **Ends** on `idle`/`failed` when **(a)** it is id-bearing and matches the open
   response (or no id-bearing open was ever seen), or **(b)** it is id-less *and*
   no id-bearing response is open *and* the turn has produced something.
3. **Ignores** an id-less `idle` while an id-bearing response is open (the
   mid-answer flap), and an id-less `idle` before anything was produced (the
   cold-start flap).
4. Never ends on `waiting` (both harnesses use it for "parked on sub-agents").
5. Ignores a terminal that arrives before the turn started — a stale status
   replayed on connect.

Explicit `response.failed`/`.cancelled` and `turn.failed`/`.cancelled` are
hard-terminals too. `tests/test_omnigent.py` has a case per branch.

### Dead-socket backstop

The stream never sends `[DONE]` and never closes on its own; the server
heartbeats roughly every 15s. So the **only** condition not signalled by an event
is a dead (half-open) socket. The loop treats "no event of any kind for
`idle_grace_seconds`" (default 600s) as a dead connection and ends. This is the
one justified client-side heuristic: a dead connection by definition can't send a
signal.

### Reconnect

A proxy max-duration cap can sever a long-lived response while the turn keeps
running server-side. That surfaces as `StreamInterruptedError` (distinct from
`ServerUnreachableError`, which means the server is actually down) and the client
re-opens the stream, carrying turn-end state across the reconnect. On re-open the
server replays the in-flight assistant text as one cumulative delta per message;
`_reconcile_delta` forwards only the unseen suffix, so the reply never
double-renders. The message is submitted **once** — re-submitting would start a
second turn.

## Rendering the answer: edit-in-place

This is the deepest Discord-specific piece, and it replaces everything Slack's
streaming API does server-side.

- **One message, edited.** `_LiveReply` opens a message showing the
  "Working on it…" placeholder and edits that same message as deltas arrive. The
  placeholder is therefore *replaced by* the answer rather than deleted
  alongside it, so the channel never shows a gap.
- **Cadence, not per-delta.** Message edits are rate-limited per channel, so
  edits happen at most once per `OMNIGENT_DISCORD_STREAM_EDIT_INTERVAL` (default
  1s). The **first** content after opening is always written immediately —
  holding it back would leave the channel looking stuck. A failed edit keeps the
  text and retries on the next flush, so a rate-limit blip loses nothing.
- **Idle flush.** When the read loop sees no event for 2s it forces a write, so a
  short burst the agent then pauses after doesn't stay invisible. This is a
  *sensitivity* window, not a display delay.
- **Rollover at 2000 chars.** When the current message would overflow, it is
  finalized at a clean break (paragraph → line → space) and the remainder
  continues in a new message prefixed `…`. A single unbroken run (a URL, a
  base64 blob) is cut at the limit rather than looping.
- **Ordering via seal.** Discord orders messages by send time, so text edited
  into a live message *after* an out-of-band post would keep appearing above it.
  Before every such post (approval card, policy/file notice, first todo) the
  reply is **sealed**: the current message is written out and released, the post
  lands after it, and the next delta opens a fresh message after *that*. A
  segment that never received content is deleted rather than left showing a
  stale placeholder above the card.
- **Tail reconciliation + no-delta fallback.** The final answer is whatever
  streamed; if the model committed a final item beyond the deltas, only the
  remainder is appended. If a turn streamed *nothing*, the newest server message
  is recovered as a last resort — guarded so it can't resurrect a prior turn's
  message (baseline compare) or re-post an answer an earlier sealed segment
  already showed (`already_delivered`).

Discord renders a markdown subset. Bold, italic, inline code, fenced code
blocks, lists, and headings all work; **tables do not** and arrive as raw pipes.

## Elicitations (tool approvals & questions): pure-push

When a turn hits an approval-gated tool call or an `AskUserQuestion`, the server
emits `response.elicitation_request` and parks. The bot handles this
**pure-push**, mirroring the web UI: it **keeps reading the stream** and observes
resolution as a normal `response.elicitation_resolved` event — it does **not**
block the read loop or poll `pending_elicitations`.

Flow (`ElicitationController`):

1. On `elicitation_request`: seal the current reply, post the card, and spawn a
   background **resolver** task — then return so the loop keeps reading.
2. The resolver awaits the click via `ElicitationCoordinator` and POSTs the
   verdict; on timeout it declines so the server-side park releases.
3. On the pushed `elicitation_resolved` (our own verdict, or an answer from the
   web UI), the card is finalized in place, exactly once (`finalized` guard). If
   the answer came from elsewhere, the coordinator wakes the resolver with a
   `RESOLVED_EXTERNALLY` sentinel so it posts nothing.

The verdict is POSTed **before** it is recorded on the pending entry, so a card
never shows "Approved" for an answer the server never received — that case is
labelled `DELIVERY_FAILED` and tells the user to re-send.

Classification is by **decision shape, not the server's delivery mode**:

- **Binary approval** → Approve/Deny buttons, with a fenced preview of the
  pending action.
- **`AskUserQuestion`** → one select menu per question (multi-select where the
  question allows it) plus Submit/Cancel. Option values carry the option
  **index**, since a label can exceed Discord's 100-character value cap; the
  index is mapped back to the full label at resolve time.
- **Anything Discord can't render faithfully** → a web-UI link, and the turn
  stays alive. That covers free-form typed input *and* a form that exceeds
  Discord's component budget (more than four questions, or more than 25 options
  in one menu). Rendering a subset would round-trip a **wrong answer** to the
  agent, which is worse than deferring.

Views are **non-persistent**: the owner/session/elicitation ids live as
attributes rather than in a component `custom_id` (capped at 100 characters,
which a session id plus an elicitation id can exceed). A non-persistent view
lives exactly as long as the in-memory coordinator waiter it answers, so a
restart drops both together.

## Mentions: deny by default

Every message the bot posts carries agent- or user-authored text — streamed
deltas, todo plans, card bodies. Discord parses mentions out of raw message
content, so without a policy a prompt could make the agent emit `<@&role>` and
ping the whole server. The client is therefore constructed with
`AllowedMentions.none()`, and the single deliberate ping (the private-notice
fallback, when a user's DMs are closed) passes its own allowance naming exactly
that one user. An approval preview additionally neutralizes triple backticks
before fencing, so prompt-controlled text can't close the block and render live.

## Concurrency: run-when-idle, two guards, no queue

There is **no client-side queue**. Whether a new owner message runs is decided by
two independent guards; both must pass:

1. **Local stream guard** (`_active_channels`, reserved synchronously before any
   await). One turn streams per conversation at a time — a second concurrent
   stream would render the same events twice. A message arriving mid-stream is
   deflected, not queued.
2. **Server-activity check** (`get_session_activity`, mirroring the web UI's send
   gate). Catches activity on the *session* the local guard can't see — e.g. a
   turn driven from the web UI. If the server reports `running`/`waiting` or a
   pending elicitation, the message is deflected with a notice.

If both pass, the turn **runs** — Discord is a full conversational surface, not
kickoff-only. A message that races the check is safe regardless: the server
buffers a mid-turn submit and runs it as a continuation.

## Errors

`_classify_turn_error` is the single source of truth mapping known errors to
user-facing messages, shared by the session-startup and mid-turn paths:

- **401** → a private "run `/omnigent config`" prompt (see below).
- **Unreachable** → "reconfigure" (`/omnigent config`).
- **No online host** → the `omni host --server …` command.
- **412 `harness_not_configured`** → the server's *curated* `error.message`.
- **Stream interrupted** → "I lost my live connection; its result may still
  arrive" — explicitly *not* the "server is down" wording.

Server error bodies are otherwise **not** echoed to the channel (they can leak
internal paths and stack traces) — only that one actionable code's message is
surfaced; everything else is logged and shown as a generic failure.

An expired login is delivered **privately**, not in the channel: the fix is a
command only that one user can run, and the channel is shared. Discord has no
ephemeral message outside an interaction, so it goes to their DM and falls back
to a self-deleting mention when DMs are closed.

## Authentication (per-user, delegated)

Each Discord user authenticates as their own Omnigent identity — no Omnigent
credential passes through Discord. The bot auto-detects the server's auth mode
(unauthenticated `GET /v1/me`) and drives `accounts`-mode device grant (RFC 8628)
or the `oidc` cli-login flow inside the ephemeral `/omnigent config` message.
Tokens are encrypted at rest when `OMNIGENT_DISCORD_TOKEN_ENCRYPTION_KEY` is set,
else in-memory only. The 401-retry path refreshes a delegated token once
mid-request, single-flighted so concurrent turns can't burn a single-use refresh
token twice.

`header`/proxy mode is **unsupported here**. The Slack integration additionally
ships a Databricks Apps web-auth mode for it (a custom U2M OAuth app behind an
enrollment page the bot serves as its own Databricks App); that is tied to
running the bot as a Databricks App and has not been ported. A Discord
deployment against a proxy-mode server needs the server in accounts/oidc mode, or
the bot behind the same identity proxy.

See `designs/DEVICE_AUTH.md` in the main repo for the threat model.

## Testing notes

Unit tests use recording fakes (`RecordingChannel`, `IncomingMessage`,
`FakeOmnigent`) that mirror the real Discord call surface — sends, edits,
deletes, thread creation — so routing and rendering are asserted from the user's
point of view. `test_integration.py` drives a **real** `OmnigentClient` against a
`respx` stand-in for the server, asserting both the HTTP requests issued and what
appeared in Discord. `test_api_spec_drift.py` reconciles the endpoints the client
calls against the repo's committed `openapi.json`, so a server-side rename fails
a Discord test rather than surfacing at runtime.

The trickiest behaviours — turn-end per harness, the dead-socket backstop,
reconnect de-duplication, seal ordering, rollover, and each elicitation outcome —
each have a regression test that fails without its fix.
