# Omnigent Slack Bot

Slack Socket Mode bot that maps one Slack thread to one Omnigent session. The
bot talks to **one** Omnigent server, set by the operator via
`OMNIGENT_SERVER_URL` — Slack users never enter a URL, so the bot only ever
issues requests to that fixed host. Each user still authenticates as their own
Omnigent identity against it.

> This README is the operator/user guide (setup, scopes, running, auth). For the
> architecture and key technical decisions, see **[DESIGN.md](DESIGN.md)**.

## Setup

1. Create a Slack app with Socket Mode **and** Interactivity enabled (Socket
  Mode delivers the interactive button/modal payloads — no request URL needed).
2. Add the OAuth scopes and event subscriptions listed under **Required scopes**
   below.
3. Add a slash command `/omnigent` (Features → Slash Commands). In Socket Mode
  the request URL is ignored, so any placeholder works.
4. Install the app into the workspace.
5. Copy `.env.example` to `.env` and fill in the two Slack tokens
  (`OMNIGENT_SLACK_BOT_TOKEN`, `OMNIGENT_SLACK_APP_TOKEN`) and your Omnigent
   server URL (`OMNIGENT_SERVER_URL`). If your server sets
   `OMNIGENT_DEVICE_CLIENT_SECRET`, set the same value here so the bot is
   accepted as an authorized device-grant client.
6. Run the bot — see **Running the bot** below.

## Required scopes

The bot uses two tokens, each carrying different scopes.

### Bot token scopes (`OMNIGENT_SLACK_BOT_TOKEN`, `xoxb-…`)

Add these under **OAuth & Permissions → Scopes → Bot Token Scopes**. All are
required for the bot's core behaviour:

| Scope | Why it's needed |
| --- | --- |
| `app_mentions:read` | Receive `app_mention` events — the only way the bot joins a channel thread. |
| `chat:write` | Post, delete, and stream replies (`chat.postMessage`, `chat.delete`, `chat.startStream`), including ephemeral setup nudges (`chat.postEphemeral`). |
| `im:write` | Open a DM with the user (`conversations.open`) to send the setup button and logout confirmation. |
| `im:history` | Read direct messages. DMs are a first-class entry point and do **not** fire `app_mention`, so without this the bot can't respond in DMs. |
| `commands` | Register and receive the `/omnigent` slash command. |
| `team:read` | Read the workspace name (`team.info`) to label the delegated-login request. |

**Channel history — add per channel type where the bot will run.** These back
the plain-`message` event; add only the ones matching where you'll use the bot:

| Scope | Channel type |
| --- | --- |
| `channels:history` | Public channels |
| `groups:history` | Private channels |
| `mpim:history` | Group DMs |

If you only use the bot via DMs and channel `@mention`s, `im:history` alone is
enough and the three channel-history scopes can be omitted.

### App-level token scope (`OMNIGENT_SLACK_APP_TOKEN`, `xapp-…`)

| Scope | Why it's needed |
| --- | --- |
| `connections:write` | Open the Socket Mode connection. Socket Mode fails to connect without it. |

### Event subscriptions

Under **Event Subscriptions → Subscribe to bot events**, add:

- `app_mention`
- `message.im` (DMs)
- `message.channels` / `message.groups` / `message.mpim` — only for the channel
  types whose history scope you added above.



## Running the bot

With the `omni` CLI installed, the Slack bot is managed as a background daemon:

```bash
omni integration slack           # run in the foreground (Ctrl-C to stop)
omni integration slack start     # run in the background (detached)
omni integration slack status    # is the background bot running?
omni integration slack stop      # stop the background bot
omni integration slack logs      # print the background bot's log path
omni integration slack logs -f   # follow the log (like tail -f)
```

`omni integration slack start` spawns a detached daemon and returns
immediately; `status`/`stop`/`logs` manage it. Running `start` again while it's
already up is a no-op that reports the existing process.

All configuration (the two Slack tokens, `OMNIGENT_SERVER_URL`, and the
optional `OMNIGENT_DEVICE_CLIENT_SECRET` / `OMNIGENT_SLACK_TOKEN_ENCRYPTION_KEY`)
comes from the environment and the `.env` file — the CLI only launches the bot.

The bot lives in the separate `omnigent-slack` package, which must be installed
**in the same environment as** `omni` for the `omni integration slack` commands
to find it. Install it as the `slack` extra of omnigent:

```bash
uv tool install "omnigent[slack]"     # or, from a source checkout: uv sync --extra slack
```

Set `LOG_LEVEL=DEBUG` in `.env` when diagnosing why Slack events are not producing replies.

## Per-user setup flow

The first time a user interacts with the bot (a channel `@mention` or a DM)
without having configured, the bot DMs them a **Set up Omnigent** button and,
for channel mentions, drops an ephemeral pointer in the thread.

The button opens a modal that connects to the operator-configured server (no
URL to enter):

1. The bot validates connectivity to `OMNIGENT_SERVER_URL`. If the server has
  authentication enabled, the modal shows a login link; once the user approves
   it in their browser the **same modal advances automatically** (see
   **Authentication** below). If the server has no online host, setup shows how
   to start one (see below) instead of continuing — a session needs a host to
   run on.
2. Pick the **agent** and **host** (both required) from menus populated by the
  server, and set the **workspace path** — an absolute directory on the host
   where each session's runner starts. It defaults to the selected host's home
   directory (resolved from the server), falling back to the bot's working
   directory only if the host can't be probed.

The choice is saved per `(Slack workspace, user)`. After that, mentioning the
bot (or DMing it) starts a session on the configured server.

## Authentication

For Omnigent servers with authentication enabled, each Slack user logs in with
their own Omnigent identity — no Omnigent credential ever passes through Slack.
Login happens inside the single `/omnigent` configuration modal, not a separate
command.

The bot **auto-detects the server's auth mode** (an unauthenticated `GET /v1/me`, exactly as the `omnigent login` CLI does) and picks the matching flow:

- `accounts` **mode** → **OAuth 2.0 Device Authorization Grant** (RFC 8628).
The modal shows a verification link + code; the user approves a consent page
in their browser. The server issues a short-lived, session-scoped delegated
token plus a rotating refresh token, so the bot silently refreshes and the
token can't reach admin endpoints. **The Omnigent server must have the device
grant enabled** (`OMNIGENT_DEVICE_GRANT_ENABLED=1` — it is default-off);
otherwise the `/oauth/*` routes are absent and accounts-mode login can't
complete. If the server sets `OMNIGENT_DEVICE_CLIENT_SECRET`, set the same
value as the bot's `OMNIGENT_DEVICE_CLIENT_SECRET` so only this authorized
socket server can drive the device flow.
- `oidc` **mode** → the server's **cli-login ticket flow** (`/auth/cli-login` +
`/auth/cli-poll`). The modal shows a login link; the user signs in at *your
IdP* in their browser. The server hands back its session JWT — the same token
a browser session gets. There is **no device grant and no refresh token**: the
session lasts its normal TTL (default 8h), after which the user logs in again.
- `header` **/ proxy mode** → **unsupported**. Identity is asserted by a trusted
upstream proxy header (e.g. `X-Forwarded-Email`), so the server mints no token
and exposes no per-user login the bot can drive; setup reports that the server
can't be logged into. Run the server in `accounts` or `oidc` mode to use the
bot with authentication, or place the bot behind the same identity proxy.

Either way the flow is the same from Slack's side:

1. During setup, when the entered server requires authentication, the modal
  shows a login link and waits.
2. The user completes login in their own browser (consent page, or your IdP).
3. The bot stores the resulting token **encrypted at rest** and attaches it on
  that user's behalf.
4. The **same modal advances automatically** to the agent / host / workspace
  picker as the now-authenticated identity — no DM, no re-running the command.

The bot reads no auth-mode config itself; the Omnigent server's own
`OMNIGENT_OIDC_*` / `OMNIGENT_AUTH_*` env vars decide its mode (see the server's
`[deploy/README.md](../../deploy/README.md#auth)`).

Set `OMNIGENT_SLACK_TOKEN_ENCRYPTION_KEY` (see `.env.example`) to persist tokens
encrypted at rest; without it tokens are kept in memory only and lost on restart
(users simply re-authenticate) — the integration works either way.

`/omnigent logout` fully resets you: it revokes your delegated token and clears
all your saved settings (agent, host, workspace, and thread→session mappings).
Run `/omnigent` afterwards to set up again.

See `designs/DEVICE_AUTH.md` in the main repo for the full design and
threat model.

Run `/omnigent` (or `/omnigent config`) any time to reopen this modal and change
your agent, host, or workspace. The server is fixed by the operator, so there's
no URL to change.

Each new session **launches a fresh runner** on the chosen host rooted at the
configured workspace — the server keeps no standing runners.

If the bot can't reach your server, it replies telling you to run `/omnigent` to
reconfigure. If no host is online (or your preferred host is offline), it replies
with the command to start one, then reconfigure:

```text
Run this on the machine you want to use, then run /omnigent:
`omni host --server <your-server-url>`
```



## Usage

Mention the bot with a message to start a session:

```text
@your-bot help me inspect this failure
```

Replies stream in live and render Markdown. Replies in that Slack thread continue
the same Omnigent session. A channel thread belongs to whoever started it; a
follow-up from a different user gets a private ("Only visible to you") note
pointing them to start their own thread.

When the agent needs you — a tool-call approval or a multiple-choice question —
it appears in the thread as an **Approve / Deny** card or a radio/checkbox
**Submit** form; answer it there (or in the web UI). A request it can't render
with buttons (free-form typed input) links out to the web UI instead.

Send another message while the bot is still replying and it privately tells you
to wait or continue in the web UI; a message to an idle thread just continues the
conversation.

For how any of this works under the hood — streaming, turn-end detection,
elicitation handling, concurrency, ordering — see **[DESIGN.md](DESIGN.md)**.

## Development

This integration is a **separate package** (`omnigent-slack`) with heavy deps
(slack_bolt, aiohttp) kept out of the core `omnigent` install. It resolves as an
editable path dep of the root `omnigent` package via the `slack` extra (see
`[tool.uv.sources]` in the root `pyproject.toml`), and shares the root's dev
tooling (ruff, mypy, pytest) and config rather than carrying its own. Work on it
from the repo-root env:

```bash
# From the repo root — add the slack extra to your existing extras:
uv sync --extra slack       # e.g. --extra all --extra dev --extra slack
uv run omni integration slack
```
