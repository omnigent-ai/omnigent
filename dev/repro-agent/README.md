# repro-agent

Reproduce a bug **live in your running Omnigent app** and capture it as a
durable end-to-end test. It runs against whatever server you already have (the
server `omnigent run` spins up, or one you pass with `--server`) and authors the
reproduction test into **this** checkout.

## Prerequisites

- A configured Claude provider (`omnigent setup` — an Anthropic API key, a
  Claude subscription, an OpenAI-compatible gateway, or a Databricks workspace).
  The agent's brain runs on the Claude Agent SDK.
- `gh` authenticated (`gh auth login`) if your `bug_url` is a GitHub issue, so
  the agent can read the report.
- Run it **from the root of your `omnigent-ai/omnigent` checkout** so the agent's
  working directory is this repo and it can author tests into `tests/e2e_ui/` or
  `tests/e2e/`.
- Optional, for reproduction recordings (skipped gracefully when absent):
  Playwright browsers (`playwright install chromium`) so the authored e2e_ui
  test can run with `--video on`, [`vhs`](https://github.com/charmbracelet/vhs)
  for CLI-journey tapes, and `ffmpeg` for `.mp4` conversion.

## Usage

```bash
# Against the server `omnigent run` spins up:
omnigent run dev/repro-agent \
  -p '{"bug_url":"https://github.com/omnigent-ai/omnigent/issues/1234"}'

# Against a server you already run:
omnigent run dev/repro-agent --server http://localhost:6767 \
  -p '{"bug_url":"https://linear.app/omnigent/issue/OMNI-1234"}'
```

The `-p` payload is the input contract — just `bug_url`. The agent always
reproduces against the running build (latest `main`), so there's no version to
pass.

### Driver script (isolated worktree)

`dev/repro.py` wraps the above: it prompts for the bug URL (or takes it as an
argument), creates an **isolated git worktree** (`repro/<slug>` branch, off your
current HEAD) so the authored test lands on its own branch without dirtying your
checkout, and runs the agent from there.

```bash
python dev/repro.py                     # prompts for the bug URL
python dev/repro.py https://github.com/omnigent-ai/omnigent/issues/1234
python dev/repro.py OMNI-1234 --server http://localhost:6767
python dev/repro.py <bug_url> --public  # share the session public-read at start
```

`--public` shares the session read-only (anyone who can reach the server) right
after it starts — useful for watching a live run or reproducing against a shared
`--server`. Off by default.

It always keeps the worktree and prints its path + branch at the end; remove it
with `git worktree remove <path>` when done.

## What it does

1. Reconstructs the user journey from the linked bug report.
2. Drives the running app through that journey — browser tools for UI bugs,
   `sys_session_*` / HTTP for backend bugs — until it observes the failure.
3. Authors a durable e2e test (`tests/e2e_ui/` for UI, PTY/pexpect for CLI
   journeys, `tests/e2e/` for backend) keyed to the concrete failure, so a fix
   has a fail→pass regression guard.
4. Records each settled facet on its user-facing surface under `recordings/<slug>/`
   — the e2e_ui test run with `--video on` for web/terminal facets, a rendered VHS
   tape for CLI facets. A reproduced facet is filmed failing (before-fix footage
   the fix step pairs with its after-fix re-recording); an already-fixed facet is
   filmed passing (proof-it-works footage). Best-effort: skipped (and noted) when
   the recorders aren't installed.
5. Checkpoints the machine-readable handoff to
   `.omnigent/repro-handoff.json` as soon as the verdict is known, updating it
   as test and recording evidence lands so an interrupted final response does
   not lose a completed reproduction.
6. Emits a single fenced ```json block (the machine-readable handoff) whose
   `verdict` is exactly one of `reproduced` / `not_reproduced` / `already_fixed`
   / `needs_more_info`, alongside the per-facet breakdown (each facet stamped
   with its `surface`), test path, recordings list, session id, journey, and
   evidence. Parse `verdict` from that block to label the issue.

It does **not** fix the bug, merge, or push — it produces a live-confirmed
reproduction plus the test and hands off. The authored test lands in your working
tree (`git status` to see it).

See `AGENTS.md` for the full operating procedure.
