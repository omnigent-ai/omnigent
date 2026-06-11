# <img src="docs/images/omnigent-logo.svg" alt="" height="38" valign="middle" /> Omnigent

Run a coding or knowledge-work agent on your laptop, pick it back up from your
phone, and share the live session with a teammate. One command stands up a local
agent server with a terminal UI and a web UI — so the same session is reachable
from your terminal, your browser, and your phone.

<p align="center">
  <img src="docs/images/omnigent-hero.png" alt="Omnigent — a supervisor agent and its sub-agents working together in one shared sandbox" width="480" />
</p>

Works with **Claude**, **Codex**, **OpenAI**, any **compatible gateway**
(OpenRouter, LiteLLM, Ollama, Azure, vLLM, …), a **Databricks** workspace, or
**any agent you describe in a small YAML file**.

---

## Why Omnigent?

Omnigent lets you:

1. **📱 Work with agents from any device — including your phone.** Coding and
   knowledge-work sessions that follow you: start in your terminal, continue
   from the browser, and pick it up on your phone. Messages, sub-agents,
   terminals, and files stay in sync everywhere.
2. **🤖 Supervise multiple agents.** Use Claude Code, Codex, and custom
   agents (defined in simple YAML files) together in the same session. Ask one
   agent to review another's work, or split a task across agents that are each
   good at different things.
3. **🔌 Bring your own model.** A first-party API key, a Claude/ChatGPT
   subscription, any compatible gateway, or a Databricks workspace — all
   first-class, no lock-in.
4. **🤝 Collaborate.** Turn on accounts and share a session so teammates can
   chat with your agent and watch it work in real time, co-drive it on your
   machine, or fork the conversation to continue on their own.
5. **☁️ Run agents in cloud sandboxes.** No laptop required: launch a
   disposable [Modal](https://modal.com) sandbox from the CLI, or let the
   server provision one per session automatically (*managed hosts*, on
   Modal or [Daytona](https://www.daytona.io)). The prebaked sandbox
   image ships Claude Code, Codex, and Pi, so every harness runs out of
   the box.
6. **🛡️ Govern your agents.** Set rules for what agents can do with policies —
   pause for your approval before risky actions, cap spend, and limit which
   tools they reach — across the whole server, one agent, or a single chat.

---

## Quick start

### 1. Install

Run the bootstrap installer — it offers to install `uv` if it's missing,
checks your Node/tmux toolchain, and installs Omnigent. Rerun the same
command later to upgrade. While the repo is private, fetch the script with the
authenticated GitHub CLI (`gh`):

```bash
gh api repos/omnigent-ai/omnigent/contents/scripts/install_oss.sh \
  -H "Accept: application/vnd.github.raw" | sh
```

<details>
<summary>Prefer to install manually?</summary>

Omnigent needs **Python 3.12+** and **`uv`**. Install it straight from the
repo (`--force` so re-running re-installs the latest and rebuilds the web UI):

```bash
uv tool install --force -q --python 3.12 git+https://github.com/omnigent-ai/omnigent.git
```

</details>

<details>
<summary>Toolchain & prerequisites (if the installer reports a missing tool)</summary>

- **`uv`** (required) — https://docs.astral.sh/uv/getting-started/installation/
  (the installer offers to set this up for you)
- **`git`** (required)
- **Node.js 22 LTS or newer** + **`npm`** — for the Claude / Codex / Pi coding
  harnesses. `omnigent run` installs the harness CLI you pick.
  https://docs.npmjs.com/downloading-and-installing-node-js-and-npm
- **`tmux`** — the native `omnigent claude` / `omnigent codex` wrappers
  launch the agent through a local tmux terminal and refuse to start without
  it (`brew install tmux` / `apt install tmux`; the installer offers to
  install it for you).
- **Databricks CLI** (optional) — only if you use a Databricks workspace as
  your model provider:
  https://docs.databricks.com/aws/en/dev-tools/cli/install

</details>

### 2. Start your first agent

`omnigent` takes you from install to a working agent in one go: you pick a
model and you're chatting in your terminal. It also launches a **local web UI**
(`http://localhost:6767`, already signed in), so you can switch to the browser —
or your phone (step 4) — any time.

> [!NOTE]
> The install puts two names for the same CLI on your PATH: `omnigent` and
> the shorter `omni`. They're interchangeable.

> [!TIP]
> On first run, Omnigent picks up any model credentials already in your
> environment — an `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, or a `claude` /
> `codex` CLI you're already logged into — and offers it as the default.

```bash
omnigent
```

Or launch a specific agent runtime, or your own agent:

```bash
# Launch a specific agent runtime, in a session your team can join:
omnigent claude                  # Claude Code
omnigent codex                   # Codex

# Or run your own agent (see "Write your own agent" below), or a bundled example
omnigent run path/to/agent.yaml
omnigent run examples/polly/               # polly — a multi-agent coding orchestrator
```

**Prefer the browser?** Start a server and register your machine as a host —
then drive everything from the web UI (New Chat → pick your machine → go), no
terminal session needed:

```bash
omnigent server start   # start the local server + web UI in the background
omnigent host           # (separate terminal) register this machine as a host
```

Check it with `omnigent server status`; stop everything with `omnigent stop`
(or `omnigent server stop` to stop just the server). To run the server in the
foreground instead (e.g. in a container), use bare `omnigent server`.

### 3. Choose & switch models

```bash
omnigent setup
```

Add a credential, set a default, or remove one — grouped by agent. Omnigent
works with four kinds of credentials:

| | Kind | What it is |
|---|---|---|
| 🔑 | **API key** | A first-party vendor key — Anthropic, OpenAI, … |
| 🎟️ | **Subscription** | A Claude Pro/Max or ChatGPT plan, via the official `claude` / `codex` CLIs |
| 🌐 | **Gateway** | Any OpenAI- or Anthropic-compatible `base_url` + key — OpenRouter, LiteLLM, Ollama, vLLM, Azure, … |
| 🧱 | **Databricks** | A Databricks workspace profile |

Defaults are per agent, so a Claude default and a Codex default coexist. You can
also switch models in the middle of a session with the `/model` command.

<details>
<summary>Bring your own gateway — OpenRouter & Ollama</summary>

When you add a **Gateway** credential, `omnigent setup` asks for a base URL and
a key. The base URL depends on which agent you're pointing it at:

| Provider | For | Base URL | Key |
|---|---|---|---|
| **OpenRouter** | Claude Code | `https://openrouter.ai/api` | your OpenRouter key (`sk-or-…`) |
| **OpenRouter** | Codex / OpenAI agents | `https://openrouter.ai/api/v1` | your OpenRouter key (`sk-or-…`) |
| **Ollama** (local) | Codex / OpenAI agents | `http://localhost:11434/v1` | any value (Ollama ignores it) |

For Claude Code, point at OpenRouter's Anthropic-compatible endpoint
(`…/api`, **not** `…/api/v1`); for Codex and the OpenAI-agents harness, use the
OpenAI-compatible `…/api/v1`.

</details>

### 4. Deploy a server (and use it from your phone)

Run Omnigent on a server with a stable URL and your sessions become reachable
from anywhere — including your phone. The web UI is built for mobile, so you get
the same chat, sub-agents, terminals, and files, in sync with your laptop.

<!-- TODO: screenshot of the web UI on a phone. -->

> [!TIP]
> No deploy needed on your own network — just open your machine's LAN address on
> your phone (e.g. `http://192.168.x.x:6767`, not `localhost`).

**Docker** — on any host (your own box, a VPS, a home server):

```bash
cd deploy/docker
./bootstrap.sh          # generates the DB password + cookie secret into .env
docker compose up -d    # Omnigent server + Postgres
```

**Deploy to Render** — one click, no local tooling. Render provisions the app
and a managed Postgres and serves it over HTTPS:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/omnigent-ai/omnigent)

See [`deploy/render/README.md`](deploy/render/README.md) for the walkthrough
(admin password, OIDC, custom domains).

For **AWS EC2** (one-command Terraform) and other targets, see
[`deploy/README.md`](deploy/README.md).

Then connect your laptop to the host so new sessions run on your machine. Sign in
once (the token is reused by `run` / `attach` / `host`):

```bash
omnigent login https://your-host
```

`login` detects the server's auth mode automatically — built-in accounts,
OIDC, header-auth proxies, and Databricks-hosted servers (a Databricks App
or a workspace API path) all work with the same command; for Databricks it
runs `databricks auth login` against the right workspace for you (requires
the `databricks` extra).

```bash
omnigent host https://your-host
```

Don't want a laptop to be the host? Run the host inside a
[Modal](https://modal.com) or [Daytona](deploy/daytona/README.md) sandbox
instead — install the extra (`pip install 'omnigent[modal]'` or
`'omnigent[daytona]'`), authenticate (`modal token new`, or set
`DAYTONA_API_KEY`), then:

```bash
omnigent sandbox create --provider modal     # or --provider daytona
omnigent sandbox connect --provider modal --sandbox-id <id> --server https://your-host
```

Sandboxes boot from the official prebaked host image
(`ghcr.io/omnigent-ai/omnigent-host:latest`), with your local checkout's
wheels overlaid on top — creation is a pull plus a wheel install, not a
full in-sandbox dependency install. The image ships the coding-harness
CLIs (`claude`, `codex`, `pi`), so agents on any harness run in the
sandbox with nothing extra to install. Set `OMNIGENT_MODAL_HOST_IMAGE`
(or `OMNIGENT_DAYTONA_HOST_IMAGE`) to use a different image ref, and
`OMNIGENT_MODAL_REGISTRY_SECRET` to name a Modal secret
(`REGISTRY_USERNAME` / `REGISTRY_PASSWORD`) for private pulls — see
[`deploy/docker/README.md`](deploy/docker/README.md#host-image---target-host).

> [!NOTE]
> Modal caps sandbox lifetime at 24 hours — re-run `create` + `connect`
> to roll the host onto a fresh sandbox. Daytona has no lifetime cap,
> but free-tier orgs restrict egress to an allowlist — see
> [deploy/daytona/README.md](deploy/daytona/README.md) for the relay
> workaround.

**Or let the server do it** — with *managed hosts*, creating a session
with `"host_type": "managed"` makes the server provision a sandbox
(Modal or [Daytona](deploy/daytona/README.md)), start a host in
it, and run the
session there. No laptop, no CLI steps per session; the sandbox is
terminated when the session is deleted.

Sandboxes boot from the official prebaked host image
(`ghcr.io/omnigent-ai/omnigent-host:latest`, published by CI from the
`host` target of [`deploy/docker/Dockerfile`](deploy/docker/Dockerfile))
— so the host starts in seconds instead of installing omnigent at
boot. Configuration is just a `sandbox:` section in the server config
(`omnigent server -c config.yaml`, or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: modal
  server_url: https://your-host        # public URL sandboxes dial back to
```

To run sandboxes from your own image instead (e.g. a fork, or extra
tooling baked in), build the same `host` target and point the config
at it:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  -t docker.io/<you>/omnigent-host:latest .
docker push docker.io/<you>/omnigent-host:latest
```

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    image: docker.io/<you>/omnigent-host:latest
```

(For private registries, set `OMNIGENT_MODAL_REGISTRY_SECRET` on the
server to the name of a Modal secret holding `REGISTRY_USERNAME` /
`REGISTRY_PASSWORD`.)

**LLM credentials for managed sessions.** A fresh sandbox has no API
keys. Park your provider credentials in a [Modal
secret](https://modal.com/secrets) and list it in the config — its env
vars are injected into every managed sandbox, and the in-sandbox host
forwards the standard harness credential vars (`ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`,
`CODEX_ACCESS_TOKEN`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
`GEMINI_API_KEY`) to its runners:

```bash
modal secret create omnigent-llm \
  ANTHROPIC_API_KEY=sk-ant-… OPENAI_API_KEY=sk-…
```

```yaml
sandbox:
  provider: modal
  server_url: https://your-host
  modal:
    secrets: [omnigent-llm]
```

Using a **Claude subscription** instead of an API key? Run
`claude setup-token` on your own machine and store the resulting
long-lived token as `CLAUDE_CODE_OAUTH_TOKEN` in the secret. A
**ChatGPT Business/Enterprise plan** works the same way via a
[Codex access token](https://developers.openai.com/codex/enterprise/access-tokens)
stored as `CODEX_ACCESS_TOKEN`. For gateway setups or other env vars
beyond the standard set, add
`OMNIGENT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2` to the secret to name
the extra vars the host should forward to runners.

**Private repositories.** Managed sessions can clone a repository as
the session workspace; for private ones, store an HTTPS token as
`GIT_TOKEN` in a Modal secret (GitLab: add `GIT_USERNAME=oauth2`) —
the host image's git credential helper picks it up for the clone and
for the agent's later fetch/push.

The full Modal guide — CLI sandboxes, custom images, LLM and git
credentials, troubleshooting — lives at
[`deploy/modal/README.md`](deploy/modal/README.md); the Daytona
managed-host guide lives at
[`deploy/daytona/README.md`](deploy/daytona/README.md).

Modal credentials come from the server's environment
(`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`, or a mounted
`~/.modal.toml`) — not the config file. Sessions created with
`POST /v1/sessions {"agent_id": ..., "host_type": "managed"}` then run
on a fresh sandbox; each sandbox authenticates back with a
server-minted, per-launch token, so no user credentials ever enter
the sandbox.

Or point a one-off run at it directly:

```bash
omnigent run path/to/agent.yaml --server https://your-host
```

### 5. Collaborate with your team

Run Omnigent locally and it's just you — single user, no login, nothing to set
up. When you're ready to bring people in, flip on **multi-user accounts** with
one environment variable — `OMNIGENT_AUTH_ENABLED=1`:

```bash
# Inline for one launch:
OMNIGENT_AUTH_ENABLED=1 omnigent server start

# Or export it for the whole shell:
export OMNIGENT_AUTH_ENABLED=1
omnigent server start
```

The **Docker deploy in [step 4](#4-deploy-a-server-and-use-it-from-your-phone)
turns it on for you** — `OMNIGENT_AUTH_ENABLED` defaults to `1` there. With
auth on, Omnigent uses built-in accounts; here's how teammates join:

**Sign in.** Open the web UI (`http://localhost:6767` locally, or your host's
URL). On a fresh server it shows a create-admin form — pick your admin
username and password there (headless deploys can pre-set it with
`OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`).

**Invite teammates.** Open **Admin → Members → Invite** to create a single-use
invite link — no email server needed. Send it over; your teammate opens it, sets
a password, and they're in. Signup is invite-only.

<!-- TODO: screenshot of Admin → Members → Invite. -->

> [!NOTE]
> Teammates need to be able to reach the server. A local server is only
> reachable on your network; for anyone off it, deploy an always-on host — see
> [step 4](#4-deploy-a-server-and-use-it-from-your-phone).

Then:

- **Share a live session.** Hit **Share** in the web UI and send the link —
  teammates watch your agent work and chat with it in real time.
- **Co-drive.** A teammate co-attaches to your running session; their messages
  execute on **your** machine. Great for pairing or handing the keyboard to a
  domain expert mid-investigation.

  ```bash
  omnigent attach <session_id>
  ```

- **Fork.** Clone a conversation onto your own machine and continue
  independently from the fork point.

  ```bash
  omnigent run --fork <session_id>
  ```

### 6. Let your team use their own logins (SSO)

With auth enabled ([step 5](#5-collaborate-with-your-team)), Omnigent uses
**built-in accounts** by default — the password + invite-link flow. Nothing else
to set up, great for small teams.

Want people to sign in with the accounts they already have — **Google, GitHub,
Okta, Microsoft**? Turn on single sign-on. On your deployed server, in
`deploy/docker/.env`:

```dotenv
# Auth is already on (OMNIGENT_AUTH_ENABLED=1) by default in the deploys here.
# Adding an OIDC issuer flips the mode to single sign-on — no extra flag.
OMNIGENT_OIDC_ISSUER=https://accounts.google.com     # or https://github.com / your Okta / Entra URL
OMNIGENT_DOMAIN=agents.yourcompany.com               # your server's domain
OMNIGENT_OIDC_CLIENT_ID=…
OMNIGENT_OIDC_CLIENT_SECRET=…
```

```bash
docker compose up -d        # restart to apply
```

That's it — your team signs in with their existing accounts, and there are no
passwords for you to manage. Nothing else about the app changes.

> [!TIP]
> The only outside step is creating an app with your provider (e.g. Google
> Cloud Console, or GitHub → Settings → Developer settings) to get the client
> ID and secret. Set its **callback URL** to `https://<your-domain>/auth/callback`.

**Decide who's allowed in** — in your server config (`/data/config.yaml`):

```yaml
allowed_domains: [yourcompany.com]    # only your company's emails can sign in
admins: [you@yourcompany.com]         # who can manage members
```

> [!TIP]
> Need to let in one outsider — say a contractor on a personal account? Set
> `OMNIGENT_OIDC_ALLOW_INVITES=1` and send them a one-time invite link, instead
> of opening up the whole allowlist.

**Already have a team on built-in accounts?** One command brings everyone across
when you switch, so they keep their sessions and admin rights:

```bash
omnigent debug migrate-accounts-to-oidc <database-url> --domain yourcompany.com
```

> [!WARNING]
> **Don't deploy a shared server in header-auth mode unless you run a trusted
> reverse proxy.** Omnigent also supports a third auth mode — `header`
> (`OMNIGENT_AUTH_PROVIDER=header`) — which takes the caller's identity from
> the `X-Forwarded-Email` request header. It exists for deployments that sit
> behind an SSO proxy (oauth2-proxy, Cloudflare Access, an ALB/OIDC listener,
> Databricks Apps) that authenticates the user and injects that header on
> every request.
>
> In header mode **the server trusts whatever that header says**. If no proxy
> sets it, requests are now rejected (`401`) rather than silently sharing one
> identity — but a *misconfigured* proxy is still dangerous: if the proxy
> doesn't **strip** any client-supplied `X-Forwarded-Email` before forwarding,
> anyone can impersonate anyone by sending the header themselves. Getting this
> wrong exposes every user's sessions, conversation history, tool output, and
> files to every other caller.
>
> **For almost everyone, use built-in `accounts` (the default in these deploys)
> or `oidc`** — both authenticate users at the server with no proxy to get
> right. Only choose `header` when you already operate a proxy you trust to set
> and sanitize the identity header, and read
> [`deploy/docker/README.md`](deploy/docker/README.md#header-proxy-mode-for-deploys-behind-an-existing-sso-proxy)
> before you do.

### 7. Govern your agents with policies

Let an agent run shell commands, edit files, or spend tokens — on your terms.
**Policies** check every action and either allow it, block it, or pause to ask
you first. You don't need a config file to use them:

- **In the web UI** — open a session's info panel to browse the available
  policies and toggle them on or off.
- **In chat** — just ask: *"add a policy that asks me before running shell
  commands."* The agent sets it up for you.

Want defaults that apply to everyone, or to a specific agent? Define them in
your server config or an agent's YAML:

```yaml
policies:
  approve_shell:
    type: function
    handler: omnigent.policies.builtins.safety.ask_on_os_tools   # ask before shell / file writes
  cap_calls:
    type: function
    handler: omnigent.policies.builtins.safety.max_tool_calls_per_session
    factory_params:
      limit: 50                    # cap how many tools one session can call
  budget:
    type: function
    handler: omnigent.policies.builtins.cost.cost_budget
    factory_params:
      max_cost_usd: 5.00           # hard spend cap...
      ask_thresholds_usd: [3.00]   # ...with a soft warning on the way
```

Policies stack across all three levels — **server-wide** (admin), **per-agent**
(developer), and **per-session** (you) — with the stricter session rules checked
first. Spend caps, GitHub/Workspace access limits, and more are all built in.

See the [policy guide](docs/POLICIES.md) for the full catalog and trust model.

---

## Write your own agent

An agent is a short YAML file — your prompt, your tools, and optional helper
sub-agents (compose several in one session and let a supervisor delegate):

```yaml
name: my_agent
prompt: You are a helpful data analyst.

executor:
  harness: claude-sdk          # or: codex, openai-agents, claude-native, codex-native

tools:
  # A local Python function (schema auto-generated from the signature)
  word_count:
    type: function
    callable: mypackage.mymodule.word_count

  # A sub-agent the supervisor can delegate to
  researcher:
    type: agent
    prompt: Search for relevant information and summarize it.
    tools:
      word_count: inherit
```

```bash
omnigent run path/to/my_agent.yaml
```

For a fuller example, see `polly` (`examples/polly/`) — a
multi-agent coding orchestrator that delegates to Claude Code and Codex
sub-agents and verifies with an independent reviewer.

See the [Agent YAML spec](docs/AGENT_YAML_SPEC.md) for the full schema.

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for how to set up your environment, run the checks, and open a pull request.
