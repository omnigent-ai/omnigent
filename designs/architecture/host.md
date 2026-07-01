> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.

# Host Daemon (`omnigent host` / service `omni-host`)

> All `path:line` anchors below were opened and confirmed against the worktree
> `master-arch-docs` (main + telemetry PR #1617). Items I could not confirm in code are tagged `(unverified)`.

## 1. Role & boundaries

The **host daemon** is a long-lived process (`omnigent host`) that runs on a *machine* (a
user laptop, or a server-provisioned managed sandbox) and lets the **server launch and
manage runner subprocesses on that machine on the server's behalf**. It is the server's
remote hand on a host it cannot reach directly: the host dials *out* to the server over one
WebSocket, and the server pushes control frames down it.

What it **owns**:
- One outbound WS "host tunnel" to the server (`omnigent/host/connect.py`).
- Spawning runner subprocesses (`python -m omnigent.runner._entry`) and watching them
  (`HostProcess._runners`, `connect.py:560`, `_handle_launch` `connect.py:744`).
- The **host→runner spawn environment** — a strict allowlist (`_RUNNER_ENV_ALLOWLIST`,
  `connect.py:203`) so the host owner's shell secrets don't leak into runners.
- Host-local **filesystem queries** the server needs before any runner exists: `stat`,
  `list_dir`, `create_dir`, and git `create_worktree` / `remove_worktree`. The host is the
  source of truth for `~` expansion — the server never expands tildes (`frames.py:206-216`).
- Host **identity** (`host_id` + name) in `~/.omnigent/config.yaml` (`identity.py:54`).

What it does **NOT** own / is NOT:
- It carries **no HTTP request/response traffic** for sessions. Runners connect to the
  server with their *own* WS tunnels; the host tunnel is control-only (`frames.py:8-11`,
  `host_registry.py:9-12`).
- It does NOT hold session/conversation state, transcripts, MCP routing, policy, or the
  brain. Once a runner is spawned it talks to the server independently — the host never sees
  the turn.
- It is NOT a sandbox itself; on managed deploys the *provider* (Modal/Daytona/k8s/…)
  provisions the box and the server execs `omnigent host` inside it.

## 2. Key files & entrypoints (verified)

| Path:line | What |
|---|---|
| `omnigent/host/frames.py:37` | `HostFrameKind` enum — all 16 wire-string kinds |
| `omnigent/host/frames.py:524` / `:700` | `encode_host_frame` / `decode_host_frame` |
| `omnigent/host/frames.py:500-521` | `_encode_payload` injects W3C `traceparent` into every frame (telemetry PR) |
| `omnigent/host/connect.py:537` | `HostProcess` — daemon lifecycle |
| `omnigent/host/connect.py:744` | `_handle_launch` — spawn a runner (harness gate, workspace check) |
| `omnigent/host/connect.py:410` | `_build_runner_env` — host→runner env allowlist |
| `omnigent/host/connect.py:1409` | `_build_connect_headers` — tunnel auth (managed token vs Bearer) |
| `omnigent/host/connect.py:1462` | `_serve_frames` — hello + receive loop |
| `omnigent/host/connect.py:1513` / `:1556` | `_handle_raw_message` / `_dispatch_host_frame` — frame routing (+ `consume_frame_span`) |
| `omnigent/host/connect.py:1269` | `run()` — reconnect loop w/ recycle classification |
| `omnigent/host/identity.py:25-38` | `HOST_TOKEN/ID/NAME` env vars + `MANAGED_HOST_TOKEN_HEADER` |
| `omnigent/server/routes/host_tunnel.py:120` | server WS endpoint `/v1/hosts/{host_id}/tunnel` |
| `omnigent/server/routes/host_tunnel.py:369` | `_receive_loop` — resolves result frames to pending futures |
| `omnigent/server/host_registry.py:136` / `:220` | `HostConnection` / `HostRegistry` (in-memory, per-replica) |
| `omnigent/server/routes/hosts.py:405` | `POST /v1/hosts/{host_id}/runners` (launch route) |
| `omnigent/server/routes/sessions.py:6035` | `_launch_runner_on_host` (relaunch path on resume/new-runner) |
| `omnigent/server/managed_hosts.py:1690` / `:1842` | `launch_managed_host` / `_arm_and_start_host` |
| `omnigent/onboarding/sandboxes/base.py:152` | `SandboxLauncher` ABC (`prepare`/`provision`/`start_host`) |
| `omnigent/onboarding/sandboxes/base.py:319-326` | injects `OMNIGENT_HOST_TOKEN/ID/NAME` + execs `omnigent host` in box |
| `omnigent/stores/host_store.py:611` | `resolve_launch_token` (SHA-256 digest auth for managed hosts) |
| `omnigent/cli.py:6645` | `omni host` CLI group |
| `omnigent/cli.py:2267` | `_build_host_daemon_env` (background-daemon env allowlist) |

## 3. Internal model

**Host side** (`HostProcess`, `connect.py:537`):
- `_runners: dict[runner_id -> _RunnerHandle]` — each handle is `(subprocess.Popen, log_path)`
  (`connect.py:522`). Runner stdout/stderr is captured to `~/.omnigent/logs/host-runner/runner-*.log`.
- `_watcher_tasks` — one `_watch_runner` task per spawned runner (`connect.py:898`); polls every
  0.5s; an exit *while still in `_runners`* is unexpected (a stop pops the entry first) → composes
  an exit error (code + log tail) and sends `host.runner_exited`.
- `_unreported_exits: dict[runner_id -> error]` — exits that raced a dead tunnel; flushed right
  after the next `host.hello` (`connect.py:1491`).
- `_ever_connected`, `_login_redirect_streak` — drive the fail-loud-vs-retry decision on auth
  redirects (`connect.py:563-567`).

**Server side** (per replica, `host_registry.py`):
- `HostRegistry._hosts: dict[host_id -> HostConnection]` — newest-wins; replacing a stale conn
  poisons its outbound queue with `None` (`host_registry.py:264-271`).
- `HostConnection` (`:136`) holds: `ws`, the `hello` frame, `owner`, an `outbound_queue`
  (drained by `_sender_loop`), `last_frame_at`, and **seven `pending_*` dicts** of
  `request_id -> asyncio.Future` (launches/stops/stats/list_dirs/create_worktrees/
  remove_worktrees/create_dirs). Each request creates a future; `_receive_loop` resolves it
  when the matching `*_result` frame arrives. This is how a synchronous-feeling REST call is
  multiplexed over the async control tunnel.
- `RunnerExitReports` (`:55`) — TTL cache (600s, 1024 entries) of `host.runner_exited` causes,
  **owner-scoped** so the runner-status endpoint can answer "offline, and here's why" without
  leaking another user's log tail.
- **Persistent** cross-replica truth is the `hosts` DB table via `HostStore` (registry is only
  "live *here*"). Liveness = `status=="online" AND updated_at >= now - 90s`
  (`host_store.py:35,98` `HOST_LIVENESS_TTL_S`); the ping loop heartbeats every 30s so a host
  that dies without a graceful close drops out of the connected set when the timestamp goes stale.

## 4. Inter-component channels

The host has exactly **one** transport edge: a single **WS control-frame tunnel** to the
server. Everything else (the runner's session traffic) is on *other* tunnels the host never
touches.

```
                    ┌──────────────────────── Server (omni-server) ────────────────────────┐
  ┌──────────┐  WS  │  /v1/hosts/{id}/tunnel  ── HostRegistry / HostConnection.pending_*   │
  │  Client  │ REST │      ▲ host.hello (host→srv, on connect)                              │
  │ TUI/Web  │─────▶│  POST /v1/hosts/{id}/runners ─┐  GET /v1/hosts/{id}/filesystem/...    │
  └──────────┘      │  POST /v1/sessions (managed)  │  POST /v1/hosts/{id}/directories      │
                    └───────────────────────────────┼──────────────────────────────────────┘
                                                     │  host.* control frames (JSON, WS text)
                              ┌──────────────────────▼─────────────────────┐
                              │        Host Daemon (omni-host)              │
                              │  _handle_launch → subprocess.Popen          │
                              └──────────────────────┬─────────────────────┘
                              env-based, ONE-WAY     │  spawn: python -m omnigent.runner._entry
                              (no fd/pipe back)       ▼
                              ┌─────────────────────────────────────────────┐
                              │  Runner (omni-runner) ── its OWN WS tunnel ──┼──▶ server (separate edge)
                              └─────────────────────────────────────────────┘
```

**Channel: Host ↔ Server — WS JSON control-frame tunnel** (NOT HTTP).
- URL `wss://<server>/v1/hosts/{host_id}/tunnel` (`connect.py:590`). Host is the WS *client*
  (dials out); server is the endpoint (`host_tunnel.py:120`).
- **Frame envelope**: every message is one WS *text* frame = a JSON object with a `kind`
  discriminator (`HostFrameKind` value) + typed fields. Encode/decode in `frames.py`. The
  telemetry PR added a centralized `_encode_payload` (`frames.py:500`) that, inside an active
  span, injects W3C `traceparent`/`tracestate` keys into the JSON (decoders ignore unknown
  keys → wire-compatible). That is how the otherwise-invisible host↔server boundary joins the
  distributed trace; the receiver re-parents via `consume_frame_span` (`connect.py:1553`).
- The tunnel **multiplexes two frame families** on one socket: `host.*` frames *and* the
  runner-tunnel `ping`/`pong` keepalive (`PingFrame`/`PongFrame`, reused from
  `runner.transports.ws_tunnel.frames`). A frame that fails `decode_host_frame` is retried as
  `decode_frame`; an unknown frame is *ignored* (forward-compatible) (`connect.py:1527-1538`,
  `host_tunnel.py:399-431`).

**The 16 HostFrameKinds** (server→host request paired with host→server `*_result`, except the
one-way `hello`/`runner_exited`):

| Kind | Dir | Carries / does |
|---|---|---|
| `host.hello` | host→srv | first frame: `version`, `frame_protocol_version` (strict-major; server refuses mismatch w/ close 4002, `host_tunnel.py:214`), `name`, **`runners`** (live runner ids → reconnect reconciliation), **`configured_harnesses`** (per-harness readiness map; `None`=unknown, never "nothing configured") (`frames.py:61-86`) |
| `host.launch_runner` (+`_result`) | srv→host | `request_id`, **`binding_token`** (server derives `runner_id` via `token_bound_runner_id`), `workspace`, `harness`. Host gate: refuse if harness not configured (`error_code=harness_not_configured`) or workspace not a dir, else `Popen` (`frames.py:89-139`, `connect.py:744`) |
| `host.stop_runner` (+`_result`) | srv→host | terminate a runner pid (SIGTERM, 5s, then kill) (`connect.py:863`) |
| `host.runner_exited` | host→srv | **one-way**, no result. Composed cause = exit code + host log path + log tail. Only failure signal for a runner that died *before* connecting its own tunnel (`frames.py:172-194`) |
| `host.stat` (+`_result`) | srv→host | session-create workspace validation; returns `exists`/`type`/`canonical_path` (realpath, defeats symlink escape). ENOENT+EACCES collapse to `exists:false` (`frames.py:196-255`, `connect.py:952`) |
| `host.list_dir` (+`_result`) | srv→host | Web-UI directory picker before any runner exists; paginated by entry-path cursor (`frames.py:289`, `connect.py:1025`) |
| `host.create_dir` (+`_result`) | srv→host | Web-UI "make a new folder" in the picker (`connect.py:1122`) |
| `host.create_worktree` / `host.remove_worktree` (+`_result`) | srv→host | fork-resume git worktree on the host repo; blocking git runs in a worker thread (`connect.py:1194,1233`) |

**Server-internal trigger → tunnel** (NOT a network edge, same process): a REST handler builds
a `HostLaunchRunnerFrame`, registers a future on `conn.pending_launches[request_id]`, and
calls `host_registry.send_text(conn, frame)` which `put_nowait`s onto the `outbound_queue`
that `_sender_loop` drains (`hosts.py:605-625`, `host_registry.py:303`). `_receive_loop`
resolves the future when `host.launch_runner_result` returns (`host_tunnel.py:433`).

**Trace evidence** (live corpus, service `omni-host`): trace `fb469fff…` and `3349da47…` show
exactly:
```
[omni-server] POST /v1/hosts/{host_id}/runners        (16ms)  payload kind=host.launch_runner, workspace=/home/dhru…
  [omni-host]   host.stat            +6.9ms (0.3ms)  payload kind=host.stat_result  exists=true type=directory
  [omni-host]   host.launch_runner   +13.2ms (2.2ms) payload kind=host.launch_runner_result status=launched runner_id=runner_token…
```
i.e. one POST does a `host.stat` (workspace validation) **then** `host.launch_runner`, both
nested under the server span via the injected `traceparent`. (Matches OBSERVABILITY.md §6.4/§10.4.)
Note: these host traces carry `session.id=None` — the launch route is keyed by `host_id`, not
the session, so the host control plane is **decoupled from any one conv's trace group**. The
runner the launch spawns then shows up as `omni-runner` in the session's own traces
(e.g. conv_32db…), which is where the host's local server got the launch workspace from.

## 5. CUJ behaviors

**Session create on an external host** (laptop running `omni host <url>`):
1. Client → `POST /v1/sessions` (or `POST /v1/hosts/{id}/runners` for the fork-resume picker).
2. Server validates workspace against the agent's `os_env.cwd` boundary via `host.stat`
   (`hosts.py:454-471` calls `validate_workspace`; the stat round-trips the tunnel). Canonical
   realpath is stored, not user input (symlink-escape defense).
3. Server atomically binds `runner_id` (UPDATE … WHERE runner_id IS NULL — closes the launch
   TOCTOU, `hosts.py:580`), persists `host_id`+workspace, then sends `host.launch_runner`.
4. Host spawns the runner; result frame returns `launched`/`failed`. Server does NOT wait for
   the runner to *connect* here — it returns `status:"launching"` and the client polls runner
   status separately (`_launch_runner_on_host` `sessions.py:6048`).
5. Runner dials its *own* tunnel; from here the host is out of the loop for the turn.

**Managed sandbox** (server provisions the box, `host_type="managed"`):
`launch_managed_host` (`managed_hosts.py:1690`): `launcher.prepare()` → `provision(name)` →
`_arm_and_start_host`: `register_managed_host` arms a SHA-256 token digest in the `hosts` row
**before** the box starts (closes the dial-back race), then `launcher.start_host(...)` execs
`OMNIGENT_HOST_TOKEN=… OMNIGENT_HOST_ID=… OMNIGENT_HOST_NAME=… omnigent host --server <url>`
**inside the sandbox** (`base.py:319-326`), detached via `run_background`. Server polls
`hosts.is_online` until the host registers (`MANAGED_HOST_ONLINE_TIMEOUT_S`). Any
post-provision failure tears the box down and revokes the token. The managed identity is
durable while the box is ephemeral: `relaunch_managed_host` (`:1764`) keeps the row, provisions
a fresh box, re-arms a new token (atomically revoking the old). `resume_managed_host` (`:2032`)
wakes a *resumable* provider (`can_resume`, persistent volume) in place instead of reprovisioning.

**Reconnect / disconnect** (`connect.py:1269` run loop):
- Backoff 0.5s→10s with jitter. **Recycle classification**: explicit `1012`/`1001` close, or a
  `no close frame`/`502` on a *remote* server (Databricks Apps ingress cycling a live WS) →
  *prompt* 0.5s reconnect so the tunnel isn't down long enough to drop a `launch_runner`. On a
  **loopback** server an abrupt drop is real (e.g. re-registration) → normal backoff, so fast
  reconnects don't *fuel* a registration flap (`connect.py:1300-1345`). ⚠️ This is a subtle,
  URL-dependent branch.
- On reconnect the host re-sends `hello` with current `runners` + freshly recomputed
  `configured_harnesses`, and flushes `_unreported_exits`. Server `on_host_connect` does
  reconcile (`host_tunnel.py:261`).
- ⚠️ **Auth-redirect fail-loud**: a host that has *never* connected and hits ≥3 consecutive
  login-page redirects raises `HostConnectError` → `omni host` exits 1 with a fix hint. An
  *already-connected* host retries redirects forever (a deploy restart never kills a live host)
  (`connect.py:635-696`, `:1286-1290`).

**Stop / cleanup**: Ctrl-C/SIGTERM → `_cleanup_runners` SIGTERMs all live runners
(`connect.py:1351`). Server side: tunnel close → `deregister` + `host_store.set_offline`
(guarded so a *failed pre-register* connect can't flip another owner's host offline,
`host_tunnel.py:307-330`).

## 6. Answers to the doc questions (host's area)

- **Host daemon's role**: see §1 — the server's remote launcher/manager of runner subprocesses
  on a machine, plus host-local fs/git/stat queries; control-plane only.
- **What flows host↔server, over what channel**: §4 — a single **WS JSON control-frame
  tunnel** (`/v1/hosts/{id}/tunnel`), NOT HTTP. Envelope = JSON object w/ `kind` + typed fields
  + injected `traceparent`; 16 `HostFrameKind`s; multiplexed with ping/pong keepalive.
- **How runners launch on a host (managed sandbox)**: §5 — token armed in DB → `omnigent host`
  execed in the box with `OMNIGENT_HOST_*` env → host dials tunnel → server sends
  `host.launch_runner` → `subprocess.Popen([python, -m, omnigent.runner._entry])`.
- **Host→runner spawn (env-based, one-way)**: `_build_runner_env` (`connect.py:410`). The runner
  inherits only `_RUNNER_ENV_ALLOWLIST` (`connect.py:203` — PATH/HOME/locale/TLS-trust +
  explicitly-justified knobs like `IS_SANDBOX`, `OMNIGENT_CONFIG_HOME/DATA_DIR`,
  `OMNIGENT_AUTH_*`, `DATABRICKS_CONFIG_PROFILE/FILE`) + prefixes (`LC_/MLFLOW_/OTEL_/OMNIGENT_OTEL_`)
  + **harness credentials** (`HARNESS_CREDENTIAL_ENV_VARS`, `connect.py:352` — `ANTHROPIC_*`,
  `OPENAI_*`, `GEMINI_API_KEY`, `CODEX_ACCESS_TOKEN`, `GIT_TOKEN`, …) + operator extras via
  `OMNIGENT_RUNNER_ENV_PASSTHROUGH`. Then it stamps wiring vars `RUNNER_SERVER_URL`,
  `OMNIGENT_RUNNER_ID`, the binding token, workspace, parent pid (`connect.py:456-460`). The
  channel is **one-way**: env at spawn + a captured-log file; there is **no pipe/fd back** —
  stdin is `/dev/null` (`connect.py:816`), stdout/stderr go to a log file, and the daemon learns
  the runner's fate only by polling `proc.poll()`. Everything else the runner needs flows over
  the runner's *own* server tunnel, not from the host.
- **Host↔server auth** (`_build_connect_headers` `connect.py:1409`; server `host_tunnel.py:134-204`):
  authenticated **before `accept()`** (no acceptance oracle, no pre-auth I/O). Two modes:
  (a) **Managed sandbox** — presents `X-Omnigent-Host-Token` (`OMNIGENT_HOST_TOKEN`); server
  resolves it via `host_store.resolve_launch_token` (SHA-256 digest, indexed equality → no
  timing oracle; expiry checked atomically; token scoped to one `host_id` → leaked token can't
  register arbitrary hosts). When present the user-token path is skipped entirely.
  (b) **User host** — mints a fresh **Databricks bearer** (or stored `omnigent login` token)
  via the runner's `_make_auth_token_factory`, **refreshed every reconnect** so long-lived
  hosts survive token expiry; the authenticated user is recorded as the host `owner`. Plus an
  `Origin: <internal-ws-origin>` header to pass the server's CSWSH guard. Cross-owner takeover
  of a `host_id` is refused with a real 409 before accept (`host_tunnel.py:177-202`), except on
  the single-user loopback server (`OMNIGENT_LOCAL_SINGLE_USER` re-owns in place).
- **Credential resolution / caching**: host bearer re-minted per reconnect (above);
  `configured_harnesses` recomputed off-loop on every (re)connect, but the **launch-time
  `harness_is_configured` check is authoritative** (`connect.py:766`, `:1484`). No long-lived
  credential cache in the host itself.

## 7. Reliability gaps / sharp edges (confirmed in code)

- 🟠 **#1022 corporate-proxy gap — CONFIRMED.** Neither the host **daemon spawn env**
  (`_build_host_daemon_env`, `cli.py:2267`: allowlists `_RUNNER_ENV_ALLOWLIST` +
  `_LOCAL_DAEMON_ENV_ALLOWLIST`/prefixes locally, or `+DATABRICKS_` remotely) nor the
  **host→runner env** (`_RUNNER_ENV_ALLOWLIST`, `connect.py:203`) contains
  `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (grep for `PROXY` in `cli.py` returns only
  Apps-ingress mentions). So a user behind a corporate proxy: the foreground `omni host` would
  inherit the proxy from its shell, but the **backgrounded daemon** and **every spawned runner**
  silently lose it, with no config knob to re-add it (operator extras `OMNIGENT_RUNNER_ENV_PASSTHROUGH`
  only cover runner *credential* vars, not the daemon-spawn allowlist). Matches CUJ-ANALYSIS L504.
- ⚠️ **Tunnel-drop window drops launches.** A `host.launch_runner` sent while the tunnel is
  mid-reconnect throws `ConnectionError` from `send_text` → server rolls back the bind and
  returns 409/504 (`hosts.py:626-645`). The recycle heuristic (§5) exists *specifically* to
  shrink this window but it is URL-shape-dependent and best-effort.
- ⚠️ **`send_text` is loop-affine, not thread-safe.** `outbound_queue.put_nowait` is only safe
  because every current caller runs on the uvicorn loop; a future off-loop caller must use
  `call_soon_threadsafe` (`host_registry.py:303-324`). Latent footgun.
- ⚠️ **Per-replica registry.** A launch must be served by the replica holding the host tunnel;
  the `hosts` table is the only cross-replica truth, and `RunnerExitReports` live only on the
  tunnel's replica (`host_registry.py:61-64`) — a status poll that lands on another replica
  can't see the exit cause.
- ⚠️ **Result futures never time out on their own.** `pending_*` futures are only resolved by a
  matching result frame or popped by the *caller's* `wait_for` timeout; a buggy/oversized frame
  the server can't decode is dropped (`host_tunnel.py:545`) and the caller waits to its own
  timeout. Bounded by caller timeouts, but the dict entry lingers until then.
- 🟢 (mitigated) Runner that dies pre-connect: `_watch_runner` + `host.runner_exited` +
  `_on_runner_exited` mark the session failed with the real cause instead of a connect-timeout
  (`connect.py:898`, `app.py:2000`). This is the one good failure-signal path.

## 8. Corrections to CUJ-ANALYSIS

CUJ-ANALYSIS covers the host **thinly** — essentially one bullet (the #1022 proxy gap, L504-505)
and scattered "host-launched" mentions. What's missing / to correct:

1. **L504-505 (#1022) is CORRECT but under-scoped.** It cites "`cli.py` daemon allowlist has no
   `HTTP(S)_PROXY`/`NO_PROXY`". Confirmed at `cli.py:2267` (`_build_host_daemon_env`) — but the
   gap is *two* allowlists: the host→**runner** env (`connect.py:203 _RUNNER_ENV_ALLOWLIST`) is
   equally proxy-blind, so even if the daemon had a proxy, spawned runners still wouldn't. The
   doc should name both layers.
2. **The entire host **control protocol** is absent from CUJ-ANALYSIS.** There is no description
   of the WS control-frame tunnel, the 16 `HostFrameKind`s, the `traceparent`-in-JSON
   propagation, the request/result `pending_*` future multiplexing, or the dual auth modes
   (managed-token vs user-bearer). This is the host's core and should be a first-class section
   alongside the runner-tunnel write-up.
3. **Managed-sandbox launch lifecycle is uncovered.** The arm-token-before-start race-closure
   (`managed_hosts.py:1888-1898`), durable-identity / ephemeral-box `relaunch`/`resume`
   distinction, and the `OMNIGENT_HOST_*` env-injected `omnigent host` exec inside the box
   (`base.py:319-326`) are all unmentioned. Any CUJ touching managed/web-UI sandboxes silently
   depends on this path. Also worth noting: the host's `session.id` is `None` on its own traces,
   so host activity won't appear under a conv's trace group — a gotcha for anyone tracing a
   managed session end-to-end.
