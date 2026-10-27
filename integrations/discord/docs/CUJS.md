# Critical User Journeys — Omnigent Discord bot

The user-facing journeys the bot supports, with the canonical Omnigent terms and
pointers to the code that implements each. This is the behaviour contract; the
operator guide lives in the [README](../README.md) and the architecture in
[DESIGN.md](../DESIGN.md).

## Terminology

Terms used the way the Omnigent codebase uses them:

- **Enrollment** — linking a Discord user to their own **Omnigent identity** on
  the operator-fixed server, yielding a **delegated token** (an access + refresh
  bearer) the bot stores encrypted and presents on that user's behalf. Not
  "login/account creation" — the Omnigent account already exists; enrollment
  authorizes the bot to act as it. Keyed on the bare Discord user snowflake,
  which is global, so **one enrollment covers every server and DM**.
- **Session** — one Omnigent conversation. The bot maps **one Discord channel →
  one session** (`ChannelKey`, keyed on the channel id — a thread id in a server,
  a DM channel id in a DM). A session has an **`owner_user_id`**: the Discord
  user who started it.
- **Runner / host** — a session runs on a **runner** launched on a **host**; the
  server keeps no standing runners (each session spawns one on demand).
- **Elicitation** — a server-initiated request that parks the turn awaiting the
  user: a **tool-call approval** (Approve/Deny) or an **AskUserQuestion** (a
  multiple-choice form). Resolved with a **verdict**.
- **SessionActivity** — the server-authoritative send-gate snapshot:
  **`is_busy`** (a turn is running/waiting) and **`needs_user_action`** (parked
  on a pending elicitation).
- **Auth wall** — a response meaning the delegated token was rejected (a `401`,
  or a proxy `3xx`→login). Triggers a token **refresh**; a dead grant drops the
  token and prompts re-enrollment.

---

## 1. Setup (enrollment)

Link a Discord user to their Omnigent identity on the server, so the bot can run
turns as them. Implemented in `setup.py` (the `/omnigent config` flow) +
`auth_manager.py` / `oauth.py` (the auth flows). The bot auto-detects the
server's auth mode and drives its device-grant or OIDC-ticket login.

- **First interaction points at setup.** An unconfigured user who mentions the
  bot or DMs it is privately told to run `/omnigent config`
  (`setup_required_text` via `DiscordNotifier.post_private`). Discord has no
  ephemeral message outside an interaction, so "privately" means a DM, falling
  back to a self-deleting mention in the channel when their DMs are closed.
- **`/omnigent config` runs the whole flow in one ephemeral message.** It
  validates the server, posts a sign-in link if the server needs auth, polls for
  the delegated token to land, then updates itself to the agent / host picker and
  a workspace modal — no re-running the command. Only that user can see or use
  the message.
- **The choice is global to the user.** Discord snowflakes are workspace-
  independent, so configuring once works in every server and DM the bot shares
  with that person.
- **`/omnigent logout` unlinks the Discord user.** Revokes the grant on the
  server (best-effort) and clears all stored state for that user — delegated
  token, agent/host/workspace config, and channel→session mappings
  (`run_logout` → `AuthManager.logout_all` + `store.clear_user_data`).

## 2. DM — direct conversation

A 1:1 DM is a first-class entry point (`DiscordOmnigentService.handle_message`,
DM branch).

- **No mention needed.** Every DM message is a turn for that user.
- **One standing session per DM.** Discord DMs have **no threads**, so the DM
  channel itself is the session key — the one place the Discord model genuinely
  cannot mirror Slack's thread-per-session.
- **`/omnigent new` ends it and starts fresh.** This is the DM equivalent of
  opening a new thread: the mapping is forgotten and the next message begins a
  new session (`start_new_session`). If a turn is still streaming it **cancels
  it** — refusing would be a dead end, since in a DM this command is the only
  reset there is. It refuses for anyone but the owner.
- **The command must be registered globally to exist here.** Discord routes DM
  interactions only to global commands, so setting
  `OMNIGENT_DISCORD_COMMAND_GUILD_IDS` removes `/omnigent` from DMs entirely.

## 3. Servers — a thread per session

In a server channel the bot only engages when explicitly mentioned.

- **A mention starts a session in its own thread.** The bot creates a thread on
  the mentioning message (named after the prompt) and runs the session there, so
  a live-edited answer never takes over the channel. `owner_user_id` is the
  mentioner (`_open_thread` → `_route_turn`).
- **Inside that thread the owner needs no further mentions.** The thread exists
  for the session, so plain messages from the owner continue it. This is a
  deliberate divergence from Slack, where every channel turn needs an
  `@`-mention.
- **Bystanders are left alone.** Someone else chatting in the thread is ignored
  silently — no notice, no noise. Someone else who **mentions the bot** there
  addressed it directly, so they get a private "start your own thread" note
  (`notify_non_owner`).
- **Missing thread permission is explained.** Without *Create Public Threads* the
  bot cannot start a session in a channel; it says exactly that in the channel
  rather than failing silently (`_NO_THREAD_PERMISSION_TEXT`).
- **Only approved servers.** With `OMNIGENT_DISCORD_GUILD_IDS` set, messages from
  any other guild channel are dropped before anything else happens. This bounds
  guild channels, **not** DMs — a DM carries no guild to filter on, so turning
  *Public Bot* off in the Developer Portal is what actually limits who can add
  the bot and reach it at all.

## 4. Error handling

All surfaced with actionable guidance; raw server error detail is never echoed
into a channel (it may carry stack traces / internal paths — see
`GENERIC_FAILURE_TEXT`).

- **Auth expiry / dead grant.** On an **auth wall** the bot refreshes the
  delegated token and retries transparently (`ClientAuth.refresh` via
  `_is_auth_wall`), single-flighted so concurrent turns can't burn a single-use
  refresh token twice. Only when the grant can no longer be refreshed is the
  token dropped and the user privately told to run `/omnigent config`
  (`_AuthExpired` → `relogin_required_text`) — the fix is a command only they can
  run, and the channel is shared.
- **Server busy.** When `SessionActivity.is_busy`, a new message is not run and
  not queued; the owner gets a private notice to wait or continue in the web UI
  (`notify_busy`, `needs_action=False`). Re-sending once the session frees works.
- **Pending user action.** When `SessionActivity.needs_user_action` (the session
  is parked on an approval or question), the user is told to **answer the pending
  request above** — a distinct notice from the generic "still working" one. This
  fires **whether or not the parked turn is still streaming in this process**: a
  parked turn holds the in-process channel reservation, so that branch also
  consults `SessionActivity` to pick the right notice.
- **Messaging into someone else's session.** A conversation belongs to its
  `owner_user_id`; anyone else is refused with a private note. Enforced two ways:
  the stored-session owner check (fail-closed when the owner is unknown) and, for
  a thread whose session record is gone, the thread's **starter message** author
  — Discord's own ground truth, so a restart can't make a thread adoptable.
- **An unanswered request is declined after 3 minutes**, so a session is never
  left parked; the card says so and re-sending starts a fresh attempt.
- **A request the bot can't render.** Free-form typed input, or an
  `AskUserQuestion` larger than Discord's component budget (more than four
  questions, or more than 25 options in one menu), is surfaced as a web-UI link
  rather than a partial form — a partial form would round-trip a **wrong answer**
  to the agent. The turn stays alive and resumes once answered there.
- **A verdict that never reached the server.** If the POST of an approval fails,
  the card says so ("Couldn't be delivered") and tells the user to re-send —
  never a false "Approved" for something the server is still parked on.

Related failure surfaces the bot also handles: server unreachable (prompts
`/omnigent config`), no online host (`HostUnavailableError` → how to bring one
online), harness-not-configured on the host (`HarnessNotConfiguredError`, 412 —
surfaces the server's actionable message), and a live stream severed by a proxy
duration cap (reconnects transparently; only after reconnects are exhausted does
it say the live connection was lost, explicitly *not* "the server is down").
