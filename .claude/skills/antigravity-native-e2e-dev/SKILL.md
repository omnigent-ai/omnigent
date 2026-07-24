---
name: antigravity-native-e2e-dev
description: Spin up a live local Omnigent server + runner and exercise the native Antigravity (agy) TUI harness (antigravity-native) end-to-end — launch the real `agy` CLI via `omnigent antigravity`, drive turns through the web UI, smoke-test, and bug-bash. Load when developing, testing, or debugging the antigravity-native harness (omnigent/inner/antigravity_native_executor.py, omnigent/antigravity_native.py, antigravity_native_bridge.py, antigravity_native_rpc.py, antigravity_native_reader.py, antigravity_native_launch.py) or its agy launch / RPC mirror / tmux delivery / OAuth / MCP-relay behavior. NOT the in-process `antigravity` Gemini SDK harness.
---

# Antigravity native harness: end-to-end dev & testing (local server/runner)

The `antigravity-native` harness wraps the **real Antigravity `agy` TUI** (the
`agy` CLI, installed from `antigravity.google/cli/install.sh`). `omnigent
antigravity` ensures a host daemon, the daemon-spawned **runner** launches `agy`
in a runner-owned **tmux** terminal, and your TTY attaches to it. This is **not**
the in-process `antigravity` Gemini-SDK harness — that one runs `google-antigravity`
with a Gemini *API key*; this one drives the OAuth-only `agy` CLI and mirrors it
over **connect-RPC**. This skill is the proven recipe for running it **for real
against a live local server + runner** — not just the unit tests.

> Like the other native harnesses, the runner imports from your **current
> checkout**, so testing here exercises exactly the code you're on. (CWD/venv
> selects the code, not `PYTHONPATH`.)

## What actually runs where

```
your TTY ── (attach / pexpect) ──► omnigent antigravity (CLI, local)
                                        │ ensures
                                        ▼
                                  host daemon ──► local Omnigent server (AP)
                                        │ spawns                      ▲
                                        ▼                  connect-RPC │ HTTP
                                  runner ── launches ──► agy (TUI, in tmux)
                                        │                              │
                                        ├── write path: type web turns into the TUI
                                        │   (tmux bracketed paste → real USER_INPUT step)
                                        └── read path: RPC read driver mirrors agy's
                                            trajectory steps back into the session
```

Three transports, easy to confuse:

1. **Write path = typing into the TUI.** Every web/mobile turn is *typed* into the
   agy pane via tmux (`inject_user_message_via_tui`), creating a real
   `CORTEX_STEP_TYPE_USER_INPUT` step on the **same** cascade the TUI shows
   (#1156/#1158). It is **not** delivered over `SendUserCascadeMessage` (that
   headless RPC path was retired; the `antigravity_native.py` module header still
   says "delivered via the RPC" — that's stale doc-lag, the executor is authoritative).
2. **Read path = RPC.** `antigravity_native_reader` polls/streams agy's connect-RPC
   trajectory steps and mirrors them into the Omnigent session.
3. **Control = RPC.** Interrupt is `CancelCascadeSteps`; a tool/permission prompt
   is answered via `HandleCascadeUserInteraction` (surfaced as an Omnigent
   elicitation).

## Prerequisites (check these first)

1. **You're on the branch you want to test**, running from that checkout
   (`.venv/bin/omnigent` / `.venv/bin/python` from this repo).
2. **The `agy` CLI is on PATH** (or at `~/.local/bin/agy`) — the harness can't
   launch without it:
   ```bash
   which agy || ls -l ~/.local/bin/agy
   agy --version
   # install if missing (shell installer, NOT npm):
   #   curl -fsSL https://antigravity.google/cli/install.sh | bash   # then restart shell
   .venv/bin/python -c "from omnigent.onboarding.harness_readiness import harness_is_configured; print('antigravity-native ready:', harness_is_configured('antigravity-native'))"
   ```
3. **`agy` is signed in (OAuth).** agy is **OAuth-only** — it has no `agy login`;
   you authenticate by running bare `agy` once and completing the browser sign-in.
   It **ignores `GEMINI_API_KEY`** (API-key auth belongs to the separate
   `antigravity` SDK harness). Verify (no secrets printed):
   ```bash
   .venv/bin/python -c "from omnigent.onboarding.gemini_auth import gemini_login_detected; print('agy oauth token present:', gemini_login_detected())"
   agy models   # exits 0 and lists models only when signed in; else 'Please sign in'
   ```
   `False` / non-zero → run `agy` once and sign in. agy's token lives under
   `~/.gemini` (`oauth_creds.json` on macOS, `antigravity-cli/antigravity-oauth-token`
   on Linux).
4. **`tmux` is on PATH.** The agy terminal is a runner-owned tmux pane; the CLI
   attaches to it and the executor drives it via `tmux send-keys`
   (`_preflight_local_tools` hard-fails without tmux).
5. **Network egress to Google's Antigravity backend.** A turn that hangs / fails
   to connect on a locked-down host is usually egress, not a harness bug.

> No `node` and no provider/gateway config are needed here (unlike pi/cursor
> native): agy is a self-hosted binary and auth is the inherited Google OAuth.

## Step 1 — start a local server (real server + runner)

```bash
cd /path/to/omnigent
.venv/bin/omni server --background          # detached managed server on a free loopback port
.venv/bin/omni server status         # prints the URL, e.g. http://127.0.0.1:6767
SERVER=http://127.0.0.1:6767         # use the printed URL below
curl -s "$SERVER/health"             # {"status":"ok"}
```

(`omnigent antigravity --server ""` also auto-spawns a persistent local server and
uses it — handy for a one-shot manual run, but a known `$SERVER` URL is better for
scripted API observation below.)

## Step 2 — launch the agy terminal against the local server

`omnigent antigravity` **attaches an interactive TUI**, so run it where you can
hold it open. Two patterns:

**A. Background terminal (recommended for scripted drives).** Launch in one
terminal, drive/observe from another:

```bash
.venv/bin/omnigent antigravity --server "$SERVER" 2>&1   # attaches the agy TUI; leave it running
# add a model:  --model gemini-2.5-pro   ;   pass-through agy args go at the end
```

It prints `Web UI: <url>` and a resume hint to stderr — grab the conversation id
(the `…/c/<conv_…>` segment) for the API calls below:

```bash
CONV=conv_xxxxxxxx   # from the "Web UI:" line / resume hint
```

**B. PTY driver (fully automated).** Drive it under `pexpect` like the
`claude-native-e2e-test` skill's `cuj_driver.py`: spawn `omnigent antigravity
--server <url>` in a PTY with `cwd=<checkout>`, capture the conv id from the
printed URL, then drive/poll the API, then **tear down the whole process tree**
(see Teardown — a pexpect Ctrl-C only *detaches* tmux).

> The runner **owns** the agy terminal: binding a runner auto-creates the
> antigravity terminal for the session, and the CLI *reattaches* rather than
> launching its own. Don't hand-launch a second `agy` against the same session —
> a double launch 500s and clobbers the runner's bridge state (web-turn injection
> then fails "bridge state is missing").

## Step 3 — drive a turn (and smoke-test)

**Via the web path (exercises `AntigravityNativeExecutor`).** Post a user message
to the running session; the runner routes it to the harness, whose `_deliver`
types it into the agy TUI (real `USER_INPUT` step):

```bash
curl -s -X POST "$SERVER/v1/sessions/$CONV/events" \
  -H 'content-type: application/json' \
  -d '{"type":"message","data":{"role":"user","content":[{"type":"input_text","text":"Reply with exactly the single word: PONG"}]}}'
```

Then **observe** the mirrored transcript (the RPC read driver posts agy's steps
back):

```bash
sleep 25
curl -s "$SERVER/v1/sessions/$CONV/items" | python -m json.tool | tail -40
```

A healthy run shows your `user` message **and** a non-empty `assistant` reply
(`PONG`) mirrored into the session — proving the full stack: server → runner →
executor → tmux paste → agy turn → connect-RPC read driver → transcript mirror.
You'll also see the prompt + reply render in the attached agy TUI (parity is the
whole point of the TUI-typing write path).

- **Type-driven smoke:** instead of the POST, type a prompt directly in the
  attached agy TUI and confirm it answers + mirrors to `…/items`.
- **Model:** select a model with agy's TUI `/model`; the next web turn echoes that
  choice (the executor reads it from the latest `USER_INPUT` step).

## Inspect the bridge (debugging)

Per-session bridge state lives under a hashed dir (keyed by *bridge id*, which
defaults to the Omnigent conversation id):

```bash
.venv/bin/python -c "from omnigent.antigravity_native_bridge import bridge_dir_for_bridge_id as d; print(d('$CONV'))"
# ~/.omnigent/antigravity-native/<sha256(bridge_id)[:32]>/
#   state.json     <- {session_id, conversation_id (agy's real UUID once minted), active_turn_id}
#   tmux.json      <- {socket_path, tmux_target} the executor types into (send-keys)
#   bridge.json    <- token for the Omnigent MCP relay (sys_* tools)
#   agy-home/.gemini/...  <- per-session ISOLATED HOME: a COPY of your OAuth token
#                            + onboarding markers + config/mcp_config.json (relay)
```

Key facts:
- agy mints its **own** UUID cascade; a fresh launch seeds an `agy_conv_*`
  **placeholder** until cold-start `StartCascade`s the real id and writes it to
  `state.json` (and PATCHes it as `external_session_id`). RPC calls against a
  placeholder are skipped — "not ready yet".
- The **isolated HOME** (`agy-home/`) is why your real `~/.gemini` is never
  touched: the relay's `mcp_config.json` and agy's per-session state live there.
  agy's `/mcp` panel should show `✓ omnigent` with the `sys_*` tools.
- Env vars: `HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR`,
  `HARNESS_ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID`.

## Targeted scenarios

| Goal | How |
|------|-----|
| Web→TUI delivery | POST a message (Step 3); confirm it renders in the agy TUI AND mirrors to `…/items` |
| Native tools (shell/edit/read) | prompt agy to create→read→edit a file + run a command; confirm it touches disk |
| Omnigent MCP relay (`sys_*`) | in the agy TUI run `/mcp` → expect `✓ omnigent`; prompt agy to `sys_session_list` / spawn a sub-agent |
| Permission elicitation | with a tool that needs approval, agy's `request-review` surfaces as an **Omnigent elicitation** (interaction bridge); answer it in the web UI and confirm the tool runs |
| Interrupt | mid-turn, hit stop in the UI → `CancelCascadeSteps` (RUNNING cascades only; a step WAITING on an interaction is unblocked by a DENY, not cancel) |
| Model echo | `/model` in the TUI, then a web turn — confirm the new model is used (latest `USER_INPUT` step's `planModel`) |
| Resume | stop, `omnigent antigravity --server "$SERVER" --resume "$CONV"`; `--resume` (no value) opens the antigravity-native picker |
| Concurrency / leaks | drive several sessions; sweep for orphaned `agy` / tmux after teardown |

## Gotchas (these cost real time)

1. **It's a TUI, not `omni run`.** Use `omnigent antigravity`. The executor only
   delivers into the live agy pane — agy must be running (attached) for a turn to
   process.
2. **`config.yaml`'s `server:` defaults to a remote server.** Always pass
   `--server "$SERVER"` (or `--server ""` for local). If a *local* server rejects
   `antigravity-native`, it's stale — restart it from your checkout
   (allowlist: `omnigent/spec/_omnigent_compat.py`).
3. **OAuth-only.** agy ignores `GEMINI_API_KEY`; if `agy models` says "sign in",
   no web turn will get a real answer. Run bare `agy` once first.
4. **tmux must be reachable from the CLI process** for the direct attach; the
   executor's send-keys run on the runner side against the advertised socket.
5. **Isolated HOME.** Don't expect your real `~/.gemini` to change — agy runs
   under `<bridge_dir>/agy-home`. Look there (and `~/.gemini/antigravity-cli` for
   agy's own conversation store) when debugging.
6. **Don't double-launch agy** for a session — the runner owns the terminal (see
   Step 2). 
7. **Turns take ~20–120s** — wrap scripted waits/`timeout` generously.
8. **Never print/echo the OAuth token.** Use the boolean/`agy models` probes.

## Code & tests

- **Executor (write path — types into the TUI):** `omnigent/inner/antigravity_native_executor.py`
- **Harness wrap (`harness: antigravity-native`):** `omnigent/inner/antigravity_native_harness.py`
- **CLI launch / daemon-runner / tmux attach:** `omnigent/antigravity_native.py`
  (`run_antigravity_native`); CLI command `antigravity(...)` in `omnigent/cli.py`
- **agy argv / auth-mode / permission flag:** `omnigent/antigravity_native_launch.py`
- **Bridge (state, tmux delivery, isolated HOME, MCP relay):** `omnigent/antigravity_native_bridge.py`
- **connect-RPC client (port discovery, send/cancel/interaction):** `omnigent/antigravity_native_rpc.py`
- **RPC read driver (trajectory mirror):** `omnigent/antigravity_native_reader.py`
- **Steps / interactions / audit:** `omnigent/antigravity_native_steps.py`,
  `omnigent/antigravity_native_interactions.py`, `omnigent/antigravity_native_audit.py`
- **OAuth detection:** `omnigent/onboarding/gemini_auth.py`
- **Design/plan docs:** `docs/antigravity-native-rpc-core-design.md`,
  `docs/antigravity-native-rpc-core-plan.md`

```bash
.venv/bin/python -m pytest \
  tests/test_antigravity_native.py \
  tests/test_antigravity_native_bridge.py \
  tests/test_antigravity_native_launch.py \
  tests/test_antigravity_native_rpc.py \
  tests/test_antigravity_native_reader.py \
  tests/test_antigravity_native_steps.py \
  tests/test_antigravity_native_interactions.py \
  tests/test_antigravity_native_audit.py \
  tests/inner/test_antigravity_native_executor.py -q
```

## Bug-bash (fan out)

Stress the harness against the same `$SERVER`: the web→TUI delivery path (lost /
duplicated turns, the attended-TUI paste race), the RPC read mirror (does every
agy step reach `…/items`? duplicates after a reader restart?), the MCP relay
(`sys_*` reachable + gated), permission elicitations, interrupt
(`CancelCascadeSteps`) vs. a WAITING-on-interaction step, model echo, resume, and
orphaned `agy`/tmux after teardown. Cross-check the API — a start failure can
leave the TUI empty while the session records an error.

## Watch-outs from the code (verify live)

- **Placeholder until cold-start.** Before agy mints its real cascade id, bridge
  state holds an `agy_conv_*` placeholder and RPC is skipped; a turn fired too
  early just queues into the TUI.
- **Permission gating is all-or-nothing + post-hoc.** agy honors only
  `--dangerously-skip-permissions` (no firing pre-tool hook), so a headless launch
  auto-bypasses and the genuine Omnigent gate is the elicitation + post-hoc audit
  (`antigravity_native_audit`), not a per-tool pre-empt.
- **Stale module header.** `antigravity_native.py`'s top docstring says web turns
  go over `SendUserCascadeMessage` RPC — the live executor types into the TUI
  instead (#1156/#1158). Trust `antigravity_native_executor.py`.

## Teardown — non-negotiable

A pexpect Ctrl-C **detaches** from tmux; the runner, tmux server, and `agy` keep
running. Tear down the process tree from the child PID (`ps --ppid …` →
SIGTERM/SIGKILL) and separately `tmux -S <sock> kill-server`. Then verify:

```bash
.venv/bin/omni server stop                 # stop the managed server + local daemon
pgrep -af "(^|/)agy( |$)|harnesses\._runner|runner\._entry|tmux"   # confirm no orphans
# clean a session's bridge dir (incl. its isolated agy HOME) if you want a reset:
# rm -rf "$(.venv/bin/python -c "from omnigent.antigravity_native_bridge import bridge_dir_for_bridge_id as d; print(d('$CONV'))")"
```

## Honesty

If you can't reach a ready agy TUI (missing `agy`, not signed in, no `tmux`,
headless limits, no egress), say so — don't claim a turn passed. The strongest
evidence is the round trip observed over the API: your `user` message **and** a
non-empty `assistant` reply mirrored into `GET /v1/sessions/$CONV/items`, plus the
turn rendering in the attached agy TUI.
