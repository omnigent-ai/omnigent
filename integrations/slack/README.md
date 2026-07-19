# Omnigent Slack Bot

Slack Socket Mode bot that maps one Slack thread to one Omnigent session. The
bot talks to **one** Omnigent server, set by the operator via
`OMNIGENT_SERVER_URL` — Slack users never enter a URL, so the bot only ever
issues requests to that fixed host. Each user still authenticates as their own
Omnigent identity against it.

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

Replies stream in live (via Slack's `chat.startStream` API) and render Markdown
server-side. If a turn runs long enough that Slack finalizes the streaming
message, the bot opens a fresh streaming reply in the same thread and keeps
going, so a long answer arrives live across as many messages as it needs.
Replies in that Slack thread continue the same Omnigent session. A channel
thread belongs to whoever started it; a follow-up `@mention` from a different
user is not added to that session — that user instead gets a private
("Only visible to you") note explaining why, and pointing them to start their
own thread.

### Multi-agent turns

`session.status: idle` is an ambiguous turn boundary, so at each idle the bot
waits before deciding the turn is over — on two timescales:

- **Settle (short, ~2s).** A single agent oscillates `running`/`idle` *while
  still streaming* its answer, with sub-second gaps between bursts. Every idle
  first waits a brief settle window for the next burst, so a reply is never
  truncated mid-answer. A genuinely final idle adds only this small tail.
- **Snapshot + poll (coarse).** If still quiet after the settle, the bot checks
  the session's rolled-up status, which reads `running` while any sub-agent
  child is still working (a fan-out orchestrator like `debby` parked between
  wake cycles). While running it polls (every 5s, up to a 10-minute cap) so a
  slow sub-agent keeps the turn alive; otherwise the turn ends.

While the agent works before the first tokens arrive, the thread shows a
"Working on it…" placeholder. It's removed only once the reply is actually on
screen — on the first streamed chunk, or after the finalizing flush for a
buffered answer — so there's never an empty gap between the placeholder
disappearing and the reply appearing.

### Approvals & questions

When the agent needs the user — a tool-call approval, or a multiple-choice
question — the turn pauses and the bot surfaces it in the thread. It renders
the server's `response.elicitation_request` in one of three ways:

- **Approval** (a gated tool call): an **Approve / Deny** card with a preview of
  the pending action. Click to resume; deny (or let it sit past the wait
  window) to refuse.
- **Question** (Claude's `AskUserQuestion` and equivalents): the choices render
  as radio buttons (or checkboxes for multi-select) with a **Submit** — the
  selected labels are sent back to the agent as its answer, exactly like the
  web UI.
- **Free-form input** (a request for typed values the bot can't collect with
  buttons): the bot posts a link to resolve the request in the Omnigent web UI,
  rather than mishandling it. The turn stays alive (via the idle grace window)
  so it resumes once you answer there.

The classification is by the *decision shape*, not the server's delivery mode.
The server defaults to `url`-mode elicitations (carrying a suggested standalone
approve page), but the bot still renders a `url`-mode approval or question
natively and posts the verdict to the resolve endpoint — only genuinely
uncollectable typed input falls back to the link.

The card is updated in place with the outcome once answered, and the "Working
on it…" placeholder is cleared while parked so it doesn't sit stale. Multiple
requests in one turn are handled in order.

This mirrors the web UI and CLI — the bot consumes `response.elicitation_request`
and posts the verdict (with any selections as `content`) back to the session's
resolve endpoint.

While a request is outstanding the turn stays open, so a message sent to that
thread meanwhile is deflected like any other mid-turn message (see below). If the
user answers the request in the web UI instead of clicking the Slack card, the
bot notices (it polls for external resolution) and continues without waiting. An
unanswered card gives up after a few minutes so it can't hold the thread open
indefinitely — the user can just re-send.

**One turn at a time per thread.** Each turn opens its own event stream, so the
bot runs one turn per thread at a time — a second concurrent stream would render
into Slack twice. There is no queue. A message that arrives *while a thread is
still streaming* is not run: the bot privately ("Only visible to you") tells the
user it's still working and to re-send once it has replied, or to continue right
now in the web UI (which accepts concurrent input and shows any pending actions).
A message to a thread that is idle again runs normally — Slack stays a full
conversational surface, not just a way to kick a session off. Messages that race
the check are safe regardless: the server buffers a message that lands mid-turn
and runs it as a continuation.

**Ordering.** A streamed reply is a single Slack message anchored to the moment
it opened, so text kept flowing into it would sort *before* any card or notice
posted mid-turn — inverting cause and effect. The bot avoids this by *sealing*
the current reply at each interruption (approval card, policy/file notice): the
answer so far ends there, the out-of-band message sorts after it, and anything
the agent says next opens a fresh reply below. So the thread reads in true
order — reply, card, continued reply — even across several approvals in one turn.

### Turn progress

Beyond the streamed answer, the bot surfaces a few other signals when the
harness emits them:

- **Thinking** — while the agent reasons before producing output, the
  placeholder switches to a "Thinking…" indicator so a long think isn't silent.
- **Plan / todos** — a task list (from harnesses that report one, e.g. Claude
  Code's `TodoWrite`) is posted once and edited in place as items progress.
- **Blocked by policy** — when a tool call is hard-blocked by policy (a DENY,
  with no approval offered), the bot posts why, so an absent action is
  explained rather than silent.
- **Produced files** — a note naming any file artifact the agent generated.

All are best-effort and never interrupt the answer stream.

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
