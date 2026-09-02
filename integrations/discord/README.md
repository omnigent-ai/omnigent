# Omnigent Discord Bot

Discord gateway bot that maps one Discord conversation to one Omnigent session.
The bot talks to **one** Omnigent server, set by the operator via
`OMNIGENT_SERVER_URL` — Discord users never enter a URL, so the bot only ever
issues requests to that fixed host. Each user still authenticates as their own
Omnigent identity against it.

> This README is the operator/user guide (setup, intents, running, auth). For the
> user-facing behaviour contract (setup, DM, channels, error handling) see
> **[docs/CUJS.md](docs/CUJS.md)**; for how it is built and why, see
> **[DESIGN.md](DESIGN.md)**.

## Setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications),
   then add a **Bot** to it and copy the bot token.
2. Under **Bot → Privileged Gateway Intents**, enable **MESSAGE CONTENT
   INTENT**. The bot *requests* this intent, so without it Discord refuses the
   connection outright: the process exits at startup with
   `PrivilegedIntentsRequired` and never appears online. *Server Members* and *Presence* stay
   off — the bot never needs them.
3. Under **Installation** (or **OAuth2 → URL Generator**), build an invite with
   the `bot` and `applications.commands` scopes and the permissions listed
   below, then add the bot to your server.
4. Set `OMNIGENT_DISCORD_BOT_TOKEN` and `OMNIGENT_SERVER_URL` as **environment
   variables**. `OMNIGENT_SERVER_URL` must be `https://` for any real host — the
   per-user delegated token rides on every request, so plaintext is refused;
   `http://localhost`, `http://127.0.0.1` and `http://[::1]` are allowed for
   local testing, plus `OMNIGENT_DISCORD_GUILD_IDS` for the server(s) you run in.
   If your Omnigent server sets `OMNIGENT_DEVICE_CLIENT_SECRET`, set the same
   value here so the bot is accepted as an authorized device-grant client. See
   **Configuration** below for how the bot reads config.
5. Install the bot: `uv pip install "omnigent[discord]"` (or, from a source
   checkout, `uv sync --extra discord`). It must land in the same environment as
   `omni`.
6. Run the bot — see **Running the bot** below.
7. In a channel the bot is in, run **`/omnigent config`** and pick an agent,
   a host, and a workspace. This is per Discord account, not per server, so
   each user does it once. See **Per-user setup flow** below.

## Required permissions and intents

### Gateway intents

Only **Message Content** is a switch you flip in the Developer Portal; the other
three are non-privileged and are requested by the bot in code.

| Intent | Why it's needed |
| --- | --- |
| **Message Content** (privileged) | Read the text of messages that mention the bot, and everything in a session thread or DM. The bot requests it, so without it the connection is refused and the process exits at startup. |
| Guilds | Resolve channels and threads. Enabled by default. |
| Guild Messages / Direct Messages | Receive messages at all. Enabled by default. |

### Bot permissions (in the invite, and in each channel it runs in)

| Permission | Why it's needed |
| --- | --- |
| View Channels | See the channel a mention arrives in. |
| Send Messages | Post replies, notices, and cards. |
| Send Messages in Threads | Continue a session inside its thread. |
| Create Public Threads | Move a channel mention into its own thread — the bot refuses to start a session in a channel without this and says so. |
| Read Message History | Resolve a thread's starter message, which is how ownership survives a restart. |
| Embed Links | Render approval / question cards and the session summary. |

The `applications.commands` scope registers `/omnigent`. It is a scope, not a
permission, and it is the most common first-run trap: a `bot`-only invite
connects to the gateway perfectly well and then fails command registration with
`403 Forbidden`, so the bot answers mentions but has no slash command. Fixing it
means **re-inviting the bot** with both scopes (re-authorizing an app already in
the server grants the missing scope in place — it doesn't duplicate anything).
The bot logs exactly this when it happens.

### Guild allow-list

Unlike Slack, **anyone with Manage Server can add a Discord bot** from an invite
link — there is no admin install ceremony scoped to one workspace. Two controls,
and you want both:

- **Turn *Public Bot* OFF** in the Developer Portal (Bot → Public Bot). This is
  what actually stops anyone but you from adding the app to a server. Do this
  first.
- Set `OMNIGENT_DISCORD_GUILD_IDS` to the server ids you run in (Discord
  Settings → Advanced → Developer Mode, then right-click the server → Copy
  Server ID). Messages from a
  guild channel elsewhere are then ignored outright. Leaving it unset lets the
  bot act in every guild it has been added to.

**The allow-list does not bound DMs.** Discord lets anyone who shares a guild
with the bot open a DM with it, and a DM carries no guild to filter on — so a
member of an unapproved guild could still start a session by DM. That is why
*Public Bot* off is the control that matters.

`/omnigent` is registered **globally** by default. Guild-scoped registration is
DM-blind, so a global registration is the prerequisite for a DM ever showing the
command. It does not appear to be sufficient: in testing, a guild-installed
bot's commands did not surface in a DM with it, and neither did another bot's
checked alongside it. Treat `/omnigent` as a server-channel command.

`OMNIGENT_DISCORD_COMMAND_GUILD_IDS` opts into per-guild registration instead,
which appears within seconds and is handy while iterating. The bot logs a
warning when you are in this mode.

## Running the bot

With the `omni` CLI installed, the Discord bot is managed like the Slack one:

```bash
omni integration discord              # run in the foreground (Ctrl-C to stop)
omni integration discord --background # run in the background (detached)
omni integration discord status       # is the background bot running?
omni integration discord stop         # stop the background bot
omni integration discord logs         # print the background bot's log path
omni integration discord logs -f      # follow the log (like tail -f)
```

`--background` spawns a detached daemon and returns immediately; running it again
while the bot is up is a no-op that reports the existing process. The Slack and
Discord daemons keep separate records, so running both at once is fine.

### Configuration

All configuration comes from **real environment variables** — the bot does
**not** read a `.env` file itself. For local dev, either export the vars or
launch under a tool that injects a `.env` — e.g.
`uv run --env-file .env omni integration discord`. In production the container
deploy sets them directly. `.env.example` documents the full set.

The bot lives in the separate `omnigent-discord` package, which must be installed
**in the same environment as** `omni` for the `omni integration discord` commands
to find it. Install it as the `discord` extra of omnigent:

```bash
uv pip install "omnigent[discord]"     # or, from a source checkout:
uv sync --extra discord
```

Set `LOG_LEVEL=DEBUG` when diagnosing why Discord events are not producing
replies; that also raises discord.py's own gateway and rate-limit logging.

## Per-user setup flow

The first time a user talks to the bot without having configured, it privately
tells them to run **`/omnigent config`** (as a DM, or a self-deleting mention if
their DMs are closed).

`/omnigent config` opens an ephemeral message — visible only to that user — that
the bot updates in place:

1. It validates connectivity to `OMNIGENT_SERVER_URL`. If the server has
   authentication enabled, the message shows a sign-in link and **updates itself
   automatically** once the user approves it in their browser (see
   **Authentication** below). If no host is online, it shows how to start one
   instead of continuing — a session needs a host to run on.
2. Pick the **agent** and **host** from menus populated by the server, then press
   **Save workspace** to enter the **workspace path** — an absolute directory on
   the host where each session's runner starts. It defaults to the selected
   host's home directory, falling back to the bot's working directory only if the
   host can't be probed.

The choice is saved per Discord user. Discord user ids are global, so **one setup
covers every server and DM** the bot shares with that person — no need to
reconfigure per community.

`/omnigent` has two more subcommands:

- **`/omnigent new`** — forget this conversation's session so the next message
  starts a fresh one. Discord DMs have no threads, so this is how you end a DM
  conversation and begin another; in a server it resets the current thread.
- **`/omnigent logout`** — revoke your delegated token and clear all your saved
  settings (agent, host, workspace, and channel→session mappings). Run
  `/omnigent config` afterwards to set up again.

## Authentication

For Omnigent servers with authentication enabled, each Discord user logs in with
their own Omnigent identity — no Omnigent credential ever passes through Discord.
Login happens inside the same ephemeral `/omnigent config` message.

The bot **auto-detects the server's auth mode** (an unauthenticated
`GET /v1/me`, exactly as the `omnigent login` CLI does) and picks the matching
flow:

- `accounts` **mode** → **OAuth 2.0 Device Authorization Grant** (RFC 8628). The
  message shows a one-click sign-in button (code prefilled) and the short code to
  confirm; the user approves a consent page in their browser. (The consent page
  **forces a fresh password entry** before it will approve — even if the user is
  already signed in — so a link the user didn't personally start can't be
  approved by reflex.) The server issues a short-lived, session-scoped delegated
  token plus a rotating refresh token, so the bot refreshes silently. **The
  Omnigent server must have the device grant enabled**
  (`OMNIGENT_DEVICE_GRANT_ENABLED=1` — it is default-off). If the server sets
  `OMNIGENT_DEVICE_CLIENT_SECRET`, set the same value as the bot's
  `OMNIGENT_DEVICE_CLIENT_SECRET` so only this authorized bot can drive the flow.
- `oidc` **mode** → the server's **cli-login ticket flow** (`/auth/cli-login` +
  `/auth/cli-poll`). The user signs in at *your IdP* in their browser and the
  server hands back its session JWT. There is **no refresh token**: the session
  lasts its normal TTL (default 8h), after which the user signs in again.
- `header` **/ proxy mode** → **not supported by this integration.** Identity is
  asserted by a trusted upstream proxy header, so the server mints no token and
  exposes no per-user login the bot can drive. Run the server in `accounts`/`oidc`
  mode, or place the bot behind the same identity proxy. (The Slack integration
  ships a Databricks Apps web-auth mode for this case; it is tied to running the
  bot as a Databricks App and has not been ported here.)

Set `OMNIGENT_DISCORD_TOKEN_ENCRYPTION_KEY` (see `.env.example`) to persist tokens
encrypted at rest; without it tokens are kept in memory only and lost on restart
(users simply sign in again) — the integration works either way.

See `designs/DEVICE_AUTH.md` in the main repo for the full design and threat
model.

Each new session **launches a fresh runner** on the chosen host rooted at the
configured workspace — the server keeps no standing runners. If no host is online
(or your preferred host is offline), the bot replies with the command to start
one:

```text
Run this on the machine you want to use, then run /omnigent config:
`omni host --server <your-server-url>`
```

## Usage

**In a server channel**, mention the bot with a message:

```text
@Omnigent help me inspect this failure
```

The bot **creates a thread** on your message and runs the session there, so a
streaming answer never takes over the channel. Inside that thread you can keep
talking **without mentioning the bot** — the thread belongs to the session. Other
people can read along; if one of them mentions the bot there, they get a private
note pointing them to start their own thread.

**In a DM**, just send a message. Discord DMs have no threads, so the DM *is* the
session — use `/omnigent new` when you want a fresh one.

Replies stream in live: the bot posts one message and edits it as the answer
arrives, rolling into a follow-up message when it passes Discord's 2000-character
limit. Discord renders a markdown subset — bold, italics, code blocks, lists and
headings all work; **tables do not** and will arrive as raw pipes.

When the agent needs you — a tool-call approval or a multiple-choice question —
it appears as an embed with **Approve / Deny** buttons or select menus plus
**Submit**; answer it there (or in the web UI). **Answer within 3 minutes**: an
unanswered card is declined automatically so the session isn't left parked, and
the card then says so. Re-send your message to try again. A request the bot can't render
faithfully — free-form typed input, or a form larger than Discord's component
limits — links out to the web UI instead of showing you a partial form.

Send another message while the bot is still replying and it privately tells you
to wait or continue in the web UI; a message to an idle conversation just
continues it.

For the full set of user-facing behaviours see
**[docs/CUJS.md](docs/CUJS.md)**.

## Running alongside the Slack bot

Both bots can run at once against the same Omnigent server, on the same
machine. They share nothing stateful: separate SQLite stores
(`omnigent_discord.sqlite3` / `omnigent_slack.sqlite3`), separate daemon
records, and separate delegated tokens. Install both extras and start each:

```bash
uv sync --extra discord --extra slack
omni integration slack --background
omni integration discord --background
omni integration discord status     # each reports its own pid
```

Three environment variables are deliberately shared, because they describe the
server rather than either bot: `OMNIGENT_SERVER_URL`,
`OMNIGENT_DEVICE_CLIENT_SECRET`, and `LOG_LEVEL`. Everything else is prefixed
`OMNIGENT_DISCORD_*` or `OMNIGENT_SLACK_*`.

A person on both platforms enrolls once per bot — the delegated tokens are
per-store, and their Discord and Slack identities are unrelated to the Omnigent
server.

## Development

This integration is a **separate package** (`omnigent-discord`) with heavy deps
(discord.py and its HTTP stack) kept out of the core `omnigent` install. It is a **sibling**
of `omnigent-slack`, not a layer on it: both are standalone bots that never
import `omnigent` core. It resolves as an editable path dep of the root
`omnigent` package via the `discord` extra (see `[tool.uv.sources]` in the root
`pyproject.toml`), and shares the root's dev tooling (Ruff, pytest) and config
rather than carrying its own. Work on it from the repo-root env:

```bash
# From the repo root — install the Discord capability and contributor tooling:
uv sync --extra discord --group dev
uv run --no-sync omni integration discord

# Run its tests (from the repo root, so the root pytest config applies):
uv run --no-sync pytest integrations/discord/tests
```

Run this suite and the Slack one **separately**, as CI does. Both packages name
their test modules the same way (`test_text.py`, `test_tokens.py`, …), so
collecting the two directories in one pytest invocation trips its
same-basename check. A bare `pytest` from the repo root is unaffected — the
root config's `testpaths` covers `tests/` only.

## Troubleshooting a first run

Set `LOG_LEVEL=DEBUG` — it also raises discord.py's own gateway and rate-limit
logging. The usual causes, in order of likelihood:

| Symptom | Cause |
| --- | --- |
| `Discord refused to register /omnigent in guild …` | The invite lacked `applications.commands`. Re-invite with both scopes. |
| Bot exits at startup, never appears online | MESSAGE CONTENT INTENT is off in the Developer Portal. |
| Bot ignores a whole server | `OMNIGENT_DISCORD_GUILD_IDS` doesn't list that server's id. |
| "I need the Create Public Threads permission" | Grant it in that channel, or DM the bot instead. |
| "No online host is available" | Start one: `omni host --server <url>` (or `omni start` locally). |
| `Discord rejected the bot token` | `OMNIGENT_DISCORD_BOT_TOKEN` is wrong or was regenerated. |
| `/omnigent` missing in DMs | Expected: a guild-installed bot's commands do not surface in DMs. Run it in a server channel. Setting `OMNIGENT_DISCORD_COMMAND_GUILD_IDS` also removes it from DMs, so unset that too. |
| Reply says the harness failed to start | The harness needs setup on the host — e.g. `claude-native` needs `tmux` installed. Check the runner log the message names. |
| Setup says the server "answered, but with an error" | It is reachable, so check that log — and that `OMNIGENT_SERVER_URL` isn't pointing at an older server still holding the port. |
