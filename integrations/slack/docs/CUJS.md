# Critical User Journeys — Omnigent Slack bot

The user-facing journeys the bot supports, with the canonical Omnigent terms and
pointers to the code that implements each. This is the behaviour contract; the
architecture and auth internals live in the [README](../README.md) and
[`DATABRICKS_APP_WEBAUTH_DESIGN.md`](DATABRICKS_APP_WEBAUTH_DESIGN.md).

## Terminology

Terms used the way the Omnigent codebase uses them:

- **Enrollment** — linking a Slack user to their own **Omnigent identity** on the
  operator-fixed server, yielding a **delegated token** (an access + refresh
  bearer) the bot stores encrypted and presents on that user's behalf. Not
  "login/account creation" — the Omnigent account already exists; enrollment
  authorizes the bot to act as it.
- **Session** — one Omnigent conversation. The bot maps **one Slack thread → one
  session** (`ThreadKey`, keyed on `(team_id, channel_id, thread_ts)`), in both
  channels and DMs. A session has an **`owner_user_id`** — the Slack user who
  started the thread.
- **Runner / host** — a session runs on a **runner** launched on a **host**; the
  server keeps no standing runners (each session spawns one on demand).
- **Elicitation** — a server-initiated request that parks the turn awaiting the
  user: a **tool-call approval** (Approve/Deny) or an **AskUserQuestion** (a
  multiple-choice form). Resolved with a **verdict**.
- **SessionActivity** — the server-authoritative send-gate snapshot:
  **`is_busy`** (a turn is running/waiting) and **`needs_user_action`** (parked
  on a pending elicitation).
- **Auth wall** — a response meaning the delegated token was rejected (a `401`,
  or a Databricks-proxy `3xx`→login). Triggers a token **refresh**; a dead grant
  drops the token and prompts re-enrollment.

---

## 1. Setup (enrollment)

Link a Slack user to their Omnigent identity on the server, so the bot can run
turns as them. Implemented in `setup.py` (modal flow) + `auth_manager.py` /
`oauth.py` / `webauth.py` (the auth flows). The exact flow depends on the
server's auth mode (`OMNIGENT_SLACK_SERVER_AUTH`): `auto` drives the server's
device-grant / OIDC-ticket login; `databricks` drives a custom U2M OAuth app
(authorization code + PKCE) — see the design doc.

- **First interaction triggers setup.** An unconfigured user who `@`-mentions the
  bot in a channel or DMs it is prompted into the setup modal
  (`SetupFlow.prompt_unconfigured`). Enrollment happens **inside the modal**: the
  bot posts a sign-in link, polls for the delegated token to land, then advances
  to the agent / host / workspace picker — no re-running the command.
- **`/omnigent` retriggers setup.** Reopens the setup modal any time to
  (re-)enroll or change the chosen agent / host / workspace
  (`_handle_config_command`). The server is operator-fixed, so there is no URL to
  change.
- **`/omnigent logout` unlinks the Slack user.** Revokes the grant on the server
  (best-effort) and clears all stored state for that user — delegated token,
  agent/host/workspace config, and thread→session mappings
  (`_handle_logout` → `AuthManager.logout_all` + `store.clear_user_data`).

## 2. DM — direct conversation

A 1:1 DM is a first-class entry point (`event_is_dm`; needs the `im:history`
scope). DMs do **not** fire `app_mention`, so the bot acts on the plain `message`
event.

- **No `@`-mention needed.** Every DM message is treated as a turn for the
  DM'ing user (`handle_message`, DM branch).
- **One session per thread**, exactly like a channel: a top-level DM starts a new
  session (keyed on its own message ts); a threaded reply continues that thread's
  session. Replies thread under the triggering message.

## 3. Channels — collaborative threads

In a channel the bot only joins a thread when explicitly mentioned (needs
`app_mentions:read`).

- **`@omnigent` starts a session owned by the mentioner.** A channel
  `app_mention` starts (or continues) the thread's session, with
  `owner_user_id` = the mentioning user (`handle_app_mention` → `_route_turn`).
- **Only `@`-mention replies reach the server.** Plain channel messages — even
  replies in a thread that already has a session — are human discussion and are
  **not** forwarded to Omnigent; only `app_mention` events drive a channel turn
  (`handle_message` drops non-DM messages).

## 4. Error handling

All surfaced to the user with actionable guidance; raw server error detail is
never echoed into a channel (it may carry stack traces / internal paths — see
`GENERIC_FAILURE_TEXT`).

- **Auth expiry / dead grant.** On an **auth wall**, the bot refreshes the
  delegated token and retries transparently (`ClientAuth.refresh` via
  `_is_auth_wall`). Only when the grant can no longer be refreshed (revoked, or
  the refresh token expired) is the token dropped and the user DM'd a **re-login
  setup button** (`_AuthExpired` → `SetupFlow.prompt_relogin`) — reliably
  delivered and actionable, unlike a thread ephemeral.
- **Server busy.** When `SessionActivity.is_busy` (a turn is already
  running/waiting for that session), a new message is not run and not queued; the
  owner gets a private notice to wait or continue in the web UI
  (`notify_thread_busy`, `needs_action=False`). Re-sending once the session frees
  works.
- **Mentioning into someone else's thread.** A channel thread belongs to its
  `owner_user_id`; a different user's `@`-mention in it is refused with a private
  "start your own thread" note (`notify_non_owner`). This is enforced two ways:
  Slack's authoritative `parent_user_id` gate, and the stored-session owner check
  (fail-closed when the owner is unknown).
- **Pending user action.** When `SessionActivity.needs_user_action` (the session
  is parked on a pending elicitation — an approval or question), a new message
  can't proceed; the user is told to **respond to the pending request above**
  (here, or in the web UI) — `notify_thread_busy` with `needs_action=True`, a
  distinct notice from the generic "still working" busy one. This must fire
  **whether or not the parked turn is still streaming in this process**: a parked
  turn holds the in-process thread reservation, so the reservation branch (not
  only the server-activity branch) consults `SessionActivity` to pick the right
  notice. (Regression guard: a bug once hardcoded that branch to the "still
  working" notice, losing the pending-request reminder — see
  `test_message_while_parked_in_process_points_to_pending_request`.)

Related failure surfaces the bot also handles: server unreachable (prompts
`/omnigent`), no online host (`HostUnavailableError` → how to bring one online),
and harness-not-configured on the host (`HarnessNotConfiguredError`, 412 —
surfaces the server's actionable message).
