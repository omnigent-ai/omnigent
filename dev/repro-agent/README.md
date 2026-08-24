# repro-agent

Reproduce a bug **live in a running Omnigent app** and capture it as a durable
end-to-end test plus before-fix footage. It runs in one of two modes, chosen
automatically at runtime (see [`AGENTS.md`](AGENTS.md) "Where you're running"):

- **LOCAL** — against whatever server you already have (the one `omnigent run`
  spins up, or one you pass with `--server`), driving the app you are connected to
  with the framework browser tools, and authoring the test into **this** checkout.
- **MANAGED (Databricks Sandbox)** — as a `host_type: managed` session on a
  server-provisioned sandbox (`lakebox` — the "Databricks Sandbox" host in the
  new-session picker), with the omnigent repo cloned in as the workspace. There is
  no app handed to it, so it **boots its own** throwaway `omnigent server` +
  runner + mock LLM on `127.0.0.1` inside the sandbox and drives that with
  headless Playwright — the same stack `tests/e2e_ui/conftest.py` boots.

## Why a nested server in managed mode

On a shared managed deployment there is no local app to drive, `browser_*` tools
don't work headless (no desktop client to relay them), and driving the shared
deployment itself would be multi-tenant blast radius. So the agent boots its own
disposable server and drives that. This is also exactly the topology the video
recorder assumes (a `tests/e2e_ui/` run against a live server), so the same run
both reproduces and records.

## Prerequisites

**LOCAL:**

- A configured Claude provider (`omnigent setup` — an Anthropic API key, a Claude
  subscription, an OpenAI-compatible gateway, or a Databricks workspace). The
  agent's brain runs on the Claude Agent SDK.
- `gh` authenticated (`gh auth login`) if your `bug_url` is a GitHub issue.
- Run it **from the root of your `omnigent-ai/omnigent` checkout** so the agent's
  working directory is this repo and it can author tests into `tests/e2e_ui/` or
  `tests/e2e/`.

**MANAGED** (deployment-side, not your laptop):

- **Databricks Sandbox (`lakebox`) configured** as a sandbox provider on the
  server (it backs the "Databricks Sandbox" host-picker option).
- **Recorders for video capture.** The Databricks-managed Lakebox image is
  platform-owned (built by the internal `execution-sandbox-images` train, no
  per-deployment `image:` override), so it does not ship the recorders. The agent
  installs them **at runtime** on its first turn — `pip install .[test]` from the
  cloned workspace + `playwright install chromium` + `ffmpeg` (see
  [`setup-recorders.sh`](setup-recorders.sh) and AGENTS.md Preflight). This is
  best-effort: if an install fails, reproduction still works and recording
  degrades to `recordings: []`. (An OSS/self-hosted deployment that owns its
  sandbox image could bake the recorders into that image instead, so the runtime
  install no-ops — a future optimization, not wired up here. It does not apply to
  Databricks-managed Lakebox, whose image is platform-owned.)
- A Claude provider configured on the deployment.
- **Injected credentials** in the sandbox environment (the caller's local env is
  not forwarded to a managed sandbox): `GH_TOKEN` / `GITHUB_TOKEN` (authenticates
  `gh` and the workspace clone) and `LINEAR_API_KEY` or `DATABRICKS_LINEAR_API_KEY`
  (for Linear tickets).

## Usage

**LOCAL:**

```bash
# Against the server `omnigent run` spins up:
omnigent run dev/repro-agent \
  -p '{"bug_url":"https://github.com/omnigent-ai/omnigent/issues/1234"}'

# Against a server you already run:
omnigent run dev/repro-agent --server http://localhost:6767 \
  -p '{"bug_url":"https://linear.app/omnigent/issue/OMNI-1234"}'
```

**MANAGED** — create a managed session with the Databricks Sandbox host and the
omnigent repository as the workspace. Via the web UI: pick **Databricks Sandbox**,
set **Repository** to `https://github.com/omnigent-ai/omnigent#<branch>`, select
this agent, and pass the bug as input. Via the API (`POST /v1/sessions`):

```json
{
  "agent_id": "<this agent>",
  "host_type": "managed",
  "sandbox_provider": "lakebox",
  "workspace": "https://github.com/omnigent-ai/omnigent#main",
  "prompt": "{\"bug_url\":\"https://github.com/omnigent-ai/omnigent/issues/1234\"}"
}
```

The `-p` / `prompt` payload is the input contract — just `bug_url` (a GitHub
issue or Linear ticket URL), plus an optional `"public": true` to share the
session read-only for watching a live run. The agent always reproduces against
the running build (latest `main` / the cloned branch), so there's no version to
pass. In MANAGED mode the **Repository** workspace is required — an empty sandbox
has no `tests/` tree, so the agent would stop with a misconfigured-workspace
error.

### Driver script — LOCAL only (isolated worktree)

`dev/repro.py` wraps the LOCAL invocation: it prompts for the bug URL (or takes it
as an argument), creates an **isolated git worktree** (`repro/<slug>` branch, off
your current HEAD) so the authored test lands on its own branch without dirtying
your checkout, and runs the agent from there.

```bash
python dev/repro.py                     # prompts for the bug URL
python dev/repro.py https://github.com/omnigent-ai/omnigent/issues/1234
python dev/repro.py OMNI-1234 --server http://localhost:6767
python dev/repro.py <bug_url> --public  # share the session public-read at start
```

`--public` shares the session read-only (anyone who can reach the server) right
after it starts. Off by default. It always keeps the worktree and prints its path
+ branch at the end; remove it with `git worktree remove <path>` when done.
(MANAGED sessions are created through the server, not this script.)

## What it does

1. Reconstructs the user journey from the linked bug report, stamping each
   sub-symptom with its user-facing surface (`web` / `terminal` / `cli` / `api`).
2. Reaches the app and drives it to the failure — **LOCAL:** the connected app
   via browser tools / `sys_session_*` / HTTP; **MANAGED:** boots a nested
   `omnigent server` + runner + mock LLM in the sandbox (waits for `/health` +
   runner `online: true`) and drives it with headless Playwright.
3. Authors a durable e2e test (`tests/e2e_ui/` for UI, PTY/pexpect for CLI,
   `tests/e2e/` for backend) keyed to the failure, so a fix has a fail→pass guard.
4. **MANAGED:** records each live facet on its surface under `recordings/<slug>/`
   — the failing e2e_ui run with `--video on` for web/terminal, a rendered VHS
   tape for cli — as before-fix footage. Best-effort; the recorders are installed
   at runtime in preflight, and any lane whose tools are missing is skipped and
   noted.
5. Emits a single fenced ```json block (the machine-readable handoff) whose
   `verdict` is exactly one of `reproduced` / `not_reproduced` / `already_fixed`
   / `needs_more_info`, alongside the per-facet breakdown (each stamped with its
   `surface`), test path, recordings list, session id, journey, and evidence.
   Parse `verdict` from that block to label the issue.

It does **not** fix the bug, merge, or push — it produces a live-confirmed
reproduction plus the test (and, in managed mode, the footage) and hands off. The
authored test lands in the working tree (`git status` to see it).

See [`AGENTS.md`](AGENTS.md) for the full operating procedure.
