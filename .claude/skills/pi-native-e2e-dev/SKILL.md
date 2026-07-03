---
name: pi-native-e2e-dev
description: Spin up a live local Omnigent server + runner and exercise the native Pi TUI harness (pi-native) end-to-end — launch the real `pi` CLI via `omnigent pi`, drive turns through the web/bridge, smoke-test, and bug-bash. Load when developing, testing, or debugging the pi-native harness (omnigent/inner/pi_native_executor.py, pi_native_harness.py, omnigent/pi_native.py, pi_native_bridge.py, pi_native_credentials.py) or its bridge / extension / auth / model behavior.
---

# Pi native harness: end-to-end dev & testing (local server/runner)

The `pi-native` harness wraps the **real Pi coding-agent TUI**
(`@earendil-works/pi-coding-agent`, the `pi` CLI). Unlike the SDK harnesses
(cursor / copilot / antigravity), it does **not** run in-process: `omnigent pi`
ensures a host daemon, the daemon spawns a **runner** that launches `pi` inside a
runner-owned **tmux** terminal, and your TTY attaches to it. Omnigent's web-UI
turns are forwarded into that live `pi` process through a **file-inbox bridge** +
a packaged **JS extension** (`pi.sendUserMessage`). This skill is the proven
recipe for running it **for real against a live local server + runner** — not
just the unit tests.

> Like the other harnesses, the runner imports from your **current checkout**, so
> testing here exercises exactly the code you're on. (CWD/venv selects the code,
> not `PYTHONPATH`.)

## What actually runs where

```
your TTY ── (attach / pexpect) ──► omnigent pi (CLI, local)
                                        │ ensures
                                        ▼
                                  host daemon ──► local Omnigent server (AP)
                                        │ spawns                      ▲
                                        ▼                             │ HTTP
                                  runner ── launches ──► pi (TUI, in tmux)
                                                              │ loads
                                                              ▼
                                                 omnigent pi-native extension (JS)
```

Two ways a turn reaches Pi — test both:

1. **Type in the TUI** (your attached terminal). Exercises Pi natively; the
   extension mirrors the transcript back to the server (`POST …/events`).
2. **Web / API message.** Server → runner → **`PiNativeExecutor.run_turn`** →
   `enqueue_user_message()` writes `inbox/<ordinal>_msg_*.json` → the resident
   extension polls the inbox → `pi.sendUserMessage(...)`. This is the
   harness-specific path most worth covering.

## Prerequisites (check these first)

1. **You're on the branch you want to test**, and running from that checkout
   (`.venv/bin/omnigent` / `.venv/bin/python` from this repo).
2. **The `pi` CLI is on PATH** — the harness can't launch without it:
   ```bash
   which pi && pi --version
   # install if missing:  npm install -g @earendil-works/pi-coding-agent
   # or point at an explicit binary:  export OMNIGENT_PI_PATH=/path/to/pi
   .venv/bin/python -c "from omnigent.onboarding.harness_readiness import harness_is_configured; print('pi-native ready:', harness_is_configured('pi-native'))"
   ```
3. **`tmux` is on PATH.** The native wrapper attaches your TTY to the
   runner-owned Pi tmux pane (`_preflight_local_tools` hard-fails without it).
4. **`node` is on PATH.** The extension is JS executed inside Pi (also required
   by the e2e extension tests). `node --version`.
5. **Auth is resolvable (booleans/ids only — never print keys).** Native Pi
   normally logs in from its own `~/.pi/agent`. Omnigent bridges the provider you
   set with `omnigent setup` instead, writing a managed per-session `models.json`
   and passing `--provider omnigent --model <resolved>`. Verify what it will use:
   ```bash
   .venv/bin/python -c "from omnigent.pi_native_credentials import resolve_pi_native_provider as r; p=r(); print('provider:', getattr(p,'provider_id',None), '| api:', getattr(p,'api',None), '| model:', getattr(p,'model',None))"
   ```
   `None` → no omnigent provider configured; Pi falls back to its own `/login`
   (run `omnigent setup`, or log into `pi` directly). A Databricks default
   resolves to the AI-Gateway `anthropic-messages` surface with a refreshed
   bearer token.
6. **Network egress to the model backend.** A turn that hangs/fails to connect on
   a locked-down host is usually egress, not a harness bug.

## Step 1 — start a local server (real server + runner)

```bash
cd /path/to/omnigent
.venv/bin/omni server start          # detached managed server on a free loopback port
.venv/bin/omni server status         # prints the URL, e.g. http://127.0.0.1:6767
SERVER=http://127.0.0.1:6767         # use the printed URL below
curl -s "$SERVER/health"             # {"status":"ok"}
```

(`omnigent pi --server ""` also auto-spawns a persistent local server and uses
it — handy for a one-shot manual run, but a known `$SERVER` URL is better for
scripted API observation below.)

## Step 2 — launch the native Pi terminal against the local server

`omnigent pi` **attaches an interactive TUI**, so run it where you can hold it
open. Two patterns:

**A. Background terminal (recommended for scripted drives).** Launch it in one
terminal and drive/observe from another:

```bash
.venv/bin/omnigent pi --server "$SERVER" 2>&1   # attaches the Pi TUI; leave it running
```

It prints `Web UI: <url>` and a resume hint to stderr — grab the conversation id
(the `…/c/<conv_…>` segment). Capture it for the API calls below:

```bash
CONV=conv_xxxxxxxx   # from the "Web UI:" line / resume hint
```

**B. PTY driver (fully automated).** Drive it under `pexpect` exactly like the
`claude-native-e2e-test` skill's `cuj_driver.py` (a proven, generalizable base):
spawn `omnigent pi --server <url>` in a PTY with `cwd=<checkout>`, capture the
conv id from the printed URL, send keystrokes / poll the API, then **tear down
the whole process tree** (see Teardown — pexpect Ctrl-C only *detaches* tmux).

Pass-through Pi CLI args go after the command (persisted as
`terminal_launch_args`), e.g. `omnigent pi --server "$SERVER" -- --model <id>`;
omnigent still injects `--provider omnigent --model <resolved>` when a provider
is configured (see `pi_native_credentials.py`).

## Step 3 — drive a turn (and smoke-test)

**Via the web/bridge path (exercises `PiNativeExecutor`).** Post a user message
to the running session; the runner routes it through the harness → bridge inbox →
extension → `pi.sendUserMessage`:

```bash
curl -s -X POST "$SERVER/v1/sessions/$CONV/events" \
  -H 'content-type: application/json' \
  -d '{"type":"message","data":{"role":"user","content":[{"type":"input_text","text":"Reply with exactly the single word: PONG"}]}}'
```

Then **observe** the mirrored transcript (the extension forwards Pi's output back
via `POST …/events`):

```bash
sleep 20
curl -s "$SERVER/v1/sessions/$CONV/items" | python -m json.tool | tail -40
```

A healthy run shows your `user` message **and** a non-empty `assistant` reply
(`PONG`) mirrored into the session — proving the full stack: server → runner →
harness → inbox → extension → Pi → transcript forwarder. You'll also see Pi
render the message in the attached TUI.

- **Type-driven smoke:** instead of the POST, type a prompt directly in the
  attached TUI and confirm it answers + mirrors to `…/items`.
- **Specific model:** see Step 2 pass-through note; confirm the resolved model in
  the Prereq-5 probe.

## Inspect the bridge (debugging)

Everything the harness writes for a session lives under a hashed bridge dir:

```bash
.venv/bin/python -c "from omnigent.pi_native import pi_bridge_dir_for_session as d; print(d('$CONV'))"
# ~/.omnigent/pi-native/<sha256(conv)[:32]>/
#   inbox/                 <- *.json user_message / interrupt payloads (poller drains + deletes)
#   sessions/              <- pi --session-dir state
#   config.json            <- sessionId, serverUrl, inboxDir, authHeaders (extension config)
#   omnigent_pi_native_extension.js
ls -la "$(.venv/bin/python -c "from omnigent.pi_native import pi_bridge_dir_for_session as d; print(d('$CONV'))")/inbox"
```

If a queued message never reaches Pi, watch whether `inbox/*.json` drains. The
managed Pi config dir (`PI_CODING_AGENT_DIR`) holds the generated `models.json`
that wires Pi's provider/model. Key env vars: `HARNESS_PI_NATIVE_BRIDGE_DIR`,
`HARNESS_PI_NATIVE_REQUEST_SESSION_ID`, `OMNIGENT_PI_NATIVE_CONFIG`,
`OMNIGENT_PI_PATH` (legacy `HARNESS_PI_PATH`), `PI_CODING_AGENT_DIR`.

## Targeted scenarios

| Goal | How |
|------|-----|
| Web→Pi delivery | POST a message (Step 3); confirm a fresh `inbox/*.json` appears then drains and the reply mirrors to `…/items` |
| Native tools (shell/edit/read) | prompt Pi to create→read→edit a file and run a shell command; confirm it touches disk |
| Resume | stop the TUI, `omnigent pi --server "$SERVER" --resume "$CONV"` — reattaches; `--resume` (no value) opens the pi-native picker |
| Interrupt | mid-turn, enqueue an interrupt (`pi_native_bridge.enqueue_interrupt(bridge_dir)`) or use the UI stop; confirm Pi's `abort()` fires and the next turn isn't poisoned (see `test_pi_native_interrupt_replay_e2e.py`) |
| Policy / guardrail | add a guardrail that denies a keyword; native Pi tool calls are gated by the extension POSTing `…/policies/evaluate` (not the turn-scoped evaluator) — confirm a DENY blocks |
| Model routing | flip the configured provider/model; re-check the Prereq-5 probe and that the answer still lands |
| Concurrency / leaks | drive several sessions; then sweep for orphaned `pi` / runner / tmux (see Cleanup) |

## Gotchas (these cost real time)

1. **It's a TUI, not `omni run`.** Use `omnigent pi`. There is no
   `omni run <bundle>` path for pi-native; the executor only enqueues into the
   bridge — Pi must be alive (attached) for a turn to be processed.
2. **`config.yaml`'s `server:` defaults to a remote server.** Always pass
   `--server "$SERVER"` (or `--server ""` to auto-spawn local). If a *local*
   server rejects `pi-native`, it's running stale code — restart it from your
   checkout (allowlist: `omnigent/spec/_omnigent_compat.py`).
3. **No live LLM without auth.** If the Prereq-5 probe prints `None` and `pi`
   isn't logged in, turns won't get a real answer. Configure a provider via
   `omnigent setup` or `pi` `/login`.
4. **tmux must be reachable from the CLI process.** Direct tmux attach needs the
   runner-owned socket visible locally; a missing socket/`tmux` fails the attach.
5. **Turns take ~20–90s** — wrap scripted waits/`timeout` generously.
6. **Never print/echo provider keys or gateway tokens.** Use the boolean/id
   probes above.

## Code & tests

- **Executor (bridge enqueue):** `omnigent/inner/pi_native_executor.py`
- **Harness wrap (`harness: pi-native`):** `omnigent/inner/pi_native_harness.py`
- **CLI launch / daemon-runner / tmux attach:** `omnigent/pi_native.py`
  (`run_pi_native`); CLI command `pi(...)` in `omnigent/cli.py`
- **Bridge (inbox, extension/config writers):** `omnigent/pi_native_bridge.py`
- **Auth/model → Pi `models.json`:** `omnigent/pi_native_credentials.py`
- **Extension (JS, polls inbox, posts events/policies):**
  `omnigent/resources/pi_native/omnigent_pi_native_extension.js`
- **Readiness gate:** `omnigent/onboarding/harness_readiness.py`

```bash
.venv/bin/python -m pytest \
  tests/test_pi_native_bridge.py \
  tests/test_pi_native_credentials.py \
  tests/test_pi_native_extension.py \
  tests/test_pi_native_interrupt_replay_e2e.py -q   # interrupt e2e needs `node`
# JS unit tests: node omnigent/resources/pi_native/omnigent_pi_native_extension.test.js
```

## Bug-bash (fan out)

Stress the harness with several scenario probes against the same `$SERVER`: the
web→inbox→extension delivery path (lost messages / inbox that won't drain),
interrupt replay semantics, native-tool policy gating, transcript-forwarder
fidelity (does every assistant block reach `…/items`?), resume/reattach, and
orphaned `pi`/runner/tmux after teardown. Cross-check the API — a start failure
can leave the TUI empty while the session records an error.

## Watch-outs from the code (verify live — not a live-bug-bash log)

- **Empty inbox = no turn.** `PiNativeExecutor` yields `TurnComplete` once the
  message is *queued*, not once Pi *answers*; the actual answer is async via the
  extension. Judge success by `…/items`, not the POST returning `queued: true`.
- **Native Pi tool calls bypass the turn-scoped evaluator.** They're gated only
  by the extension's `POST …/policies/evaluate`; if the extension's `config.json`
  lacks `serverUrl`/`authHeaders`, gating silently no-ops.
- **History on a rebuilt session** depends on Pi's own `--session-dir` state under
  the bridge dir, not on Omnigent re-injecting transcript.

## Teardown — non-negotiable

A pexpect Ctrl-C **detaches** from tmux; the runner, the tmux server, and `pi`
keep running. Tear down the process tree from the child PID
(`ps --ppid …` → SIGTERM/SIGKILL) and separately `tmux -S <sock> kill-server`
(the tmux server reparents to init). Then verify nothing lingers:

```bash
.venv/bin/omni server stop                 # stop the managed server + local daemon
pgrep -af "(^|/)pi( |$)|harnesses\._runner|runner\._entry|tmux"   # confirm no orphans
# remove a session's bridge dir if you want a clean slate:
# rm -rf "$(.venv/bin/python -c "from omnigent.pi_native import pi_bridge_dir_for_session as d; print(d('$CONV'))")"
```

## Honesty

If you can't reach a ready Pi TUI (missing `pi`, no `tmux`/`node`, no auth,
headless limits), say so — don't claim a turn passed. The strongest evidence is
the round trip observed over the API: your `user` message **and** a non-empty
`assistant` reply mirrored into `GET /v1/sessions/$CONV/items`.
