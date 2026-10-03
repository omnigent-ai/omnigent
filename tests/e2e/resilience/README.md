# Resilience lab

A disposable Omnigent topology for testing what users see when the network
misbehaves. It runs a real server, the real `omnigent host` daemon (or a bare
runner), the real Claude Code TUI and the shared mock model. Every network link
goes through a fault proxy that a test or a person can break on demand.

```text
browser / API user ──► proxies.client ──┐
host daemon ──┐                          ├──► server
runner ───────┼──────► proxies.host ─────┘
harness hooks ┘
Claude Code ─────────► proxies.model ──────► mock model
```

`proxies.host` tags each connection by its first request line: `host.tunnel`,
`runner.tunnel` or `host.http`. `proxies.client` uses `client.sse`, `client.ws`
or `client.http`. A fault can target one tag, for example only the runner
tunnel, while everything else keeps flowing. Lab probes (`lab.observer`,
`lab.snapshot`, `lab.items`) bypass the proxies, so a fault never hides the
state being asserted.

## Run it

Requires `claude` and `tmux` on `PATH`. Tests skip without them.

```sh
uv run --no-sync pytest tests/e2e/resilience -v
OMNIGENT_E2E_RESILIENCE_FULL=1 uv run --no-sync pytest tests/e2e/resilience/scenarios -n 2  # 60 s/120 s outages
```

To poke at the UI by hand, start a lab with a fault console:

```sh
uv run --no-sync python -m tests.e2e.resilience.lab            # --mode runner, --front direct
```

The console prints a session URL behind `proxies.client`. Open it, chat, and
type commands such as `restart-server 20`, `blackhole host 30 runner.tunnel`,
`sleep-host 60`, `hold` / `release` or `conns host`. `help` lists them all.

## Scenarios

Scenario scripts live in `scenarios/`. Each one implements a row family of
the matrix in [`docs/network-resilience.md`](../../../docs/network-resilience.md).
They are built from four pieces:

- `ClaudeDriver` (`lab/driver.py`) sends user turns through the client link.
  It scripts Claude's replies by a unique marker and leaves marker-named files
  in the workspace, so a scenario can tell which tool side effects ran.
  `start_tool_turn`, `start_approval_turn` and `round_trip` put a session into
  a known phase.
- `SessionWatcher` (`lab/observe.py`) records every published `session.status`
  edge and polls the snapshot directly on the server, building a timeline of
  what the user saw.
- `ScenarioReport` (`lab/report.py`) records each contract expectation as a
  check. It writes JSON and Markdown to `.omnigent/resilience/`, then fails
  with every broken check and the timeline. `python -m tests.e2e.resilience.lab.report`
  prints the saved runs as one matrix.
- `lab_factory` (`conftest.py`) starts labs. It keeps the root of a failing
  run, or of every run with `OMNIGENT_RESILIENCE_KEEP=1`.

Rows that break the contract today are `xfail(strict=True)` with the finding,
so a fix that makes one pass must also delete its marker.

## Faults

| Call | Effect |
| --- | --- |
| `proxy.blackhole(tags)` | Hold bytes and closes both ways while sockets stay open (half-open); held bytes flush on clear. |
| `proxy.refuse(tags)` | Refuse new connections. With no tags the listener closes, so the port is refused. |
| `proxy.delay(s, tags)` / `proxy.throttle(bps, tags)` | Add latency or cap bandwidth. |
| `proxy.reset(tags)` / `proxy.close(tags)` | RST or FIN live connections now. |
| `proxy.recycle(lifetime_s, tags)` | Close connections older than a lifetime, like an ingress recycling streams. |
| `proxy.sever_held(after_s, tags)` | Answer `504` to a request still waiting for a response after `after_s`, like a front-door request cap. |
| `proxy.flap(up_s=, down_s=, mode=)` | Alternate outages with healthy periods. |
| `lab.restart_server(downtime_s=, graceful=)` / `with lab.server_down()` | Deploy (SIGTERM) or crash (SIGKILL) the server. |
| `with lab.sleep_host()` | Freeze every host-side process (daemon, runners, tmux, Claude) and blackhole their links. |
| `lab.kill_runner()` | SIGKILL the runner processes. |

Faults return a handle. Use it as a context manager or call `.clear()`. Every
connection, fault and process action is appended to `<lab root>/events.jsonl`.

`LabConfig(front="ingress")`, the default, makes a stopped server answer `502`
like the Databricks Apps front door. `front="direct"` refuses connections, as
a bare local server does.

## Isolation

The lab removes ambient `OMNIGENT_*`, `DATABRICKS_*`, `ANTHROPIC_*` and proxy
settings. It points `DATABRICKS_CONFIG_FILE` at an empty file and keeps
config, data and the Claude config directory under a short `/tmp/rlab-*` root.
tmux socket paths must stay under the macOS limit.

Claude Code managed settings can pin `ANTHROPIC_BASE_URL` to a corporate
gateway, and environment variables cannot override that. Host-side processes
therefore get `HTTPS_PROXY` pointed at `proxies.model` and trust a throwaway
CA through `NODE_EXTRA_CA_CERTS`. The proxy terminates those tunnels and serves
only model API paths (`/v1/messages` and similar) from the mock. Everything
else, including telemetry, auth and update checks, gets `403`, and nothing
intercepted leaves the machine.

Claude Code also sends title and summary requests that quote the user's
message. Script a turn with `lab.script_turn(marker, replies)`. It claims the
queue by a marker in the message and requires the `Bash` tool, so those side
requests cannot consume it.

A passing test deletes its lab root. A failing test keeps it and prints its
path, logs and URLs.

## Limits

- `sleep_host` uses SIGSTOP, which keeps wall and monotonic clocks in step, so
  the runner's suspend detection does not fire. It exercises the keepalive and
  half-open path, the same path a network change without sleep takes.
- A blackholed new connection is accepted instead of hanging in SYN-SENT.
  Clients see a read timeout rather than a connect timeout.
- Only claude-native is wired so far. Other harnesses need their own model
  routing and turn scripting.
