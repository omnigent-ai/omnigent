# CUJ Answers — Host Daemon Channel

> Domain: the `omnigent host` daemon and the host↔server tunnel. Code = ground truth
> (`traces` worktree, verified 2026-06-30). Trace evidence from local Jaeger
> (`omni-host`+`omni-server`). Per-harness behavior is **harness-agnostic at this layer**
> (claude-sdk / claude-native / codex / codex-native / polly all launch the same way) —
> the only harness-aware step is the launch-time `harness_is_configured` gate.

---

## Q: Components to instrument + the channels between them (Host Daemon)

**Host Daemon ↔ Server** is one of the named boundaries to instrument (OBSERVABILITY §6.4).

- **Channel**: a single outbound **WebSocket** `wss://<server>/v1/hosts/{host_id}/tunnel`,
  carrying **JSON text control frames** — **NOT HTTP**. No OTel auto-instrumentor can see
  it, so propagation is **manual** (`inject`/`extract` into the JSON envelope).
- **Two multiplexed frame families** on the one socket: host frames (`host.*`) and
  runner-tunnel keepalive (`ping`/`pong`). (`connect.py:1527-1538`, `host_tunnel.py:399-431`)
- **Direction**: host dials out; server then drives (server→host requests). Only
  `host.hello` and `host.runner_exited` flow host→server unsolicited; all results flow
  host→server.
- **Span boundary**: server REST request (e.g. `POST /v1/hosts/{id}/runners`, FastAPI
  auto-instrumented) is the parent; the host opens a CONSUMER span per `HostFrameKind`
  (`consume_frame_span`, `telemetry.py:734`). Verified live: `omni-host host.launch_runner`
  and `omni-host host.stat` nest under `omni-server POST /v1/hosts/{host_id}/runners`.
- **Host → Runner** is a separate, deliberately *un-traced* boundary: a one-way env handoff
  at `subprocess.Popen`; the runner roots its own trace (stitched only by `session.id`).

The other channels (for cross-reference, not my scope): Runner↔Server (runner tunnel, also
WS frames), Web/TUI↔Server (HTTP + SSE + session-updates WS), Policy server.

---

## Q: API routes & message formats for the host (WS messages vs REST; streaming vs durable)

**REST routes that drive the host** (server-side producers):

| Route | Frame sent | Result | Consumer |
|---|---|---|---|
| `POST /v1/hosts/{id}/runners` (`hosts.py:405`) | `host.stat` (validation) then `host.launch_runner` | launching / 412 / 502 / 504 | fork-resume picker; binds an unbound clone |
| `POST /v1/sessions` (`sessions.py:6060`) | `host.stat` (+ optional `host.create_worktree`) then `host.launch_runner` | session row + runner | new session create |
| `GET /v1/hosts/{id}/filesystem[/{path}]` (`hosts.py:668,706`) | `host.list_dir` | `{object:list,data,has_more}` | Web UI directory picker |
| `POST /v1/hosts/{id}/directories` (`hosts.py:836`) | `host.create_dir` | created path / 409 | Web UI "new folder" |
| `GET /v1/hosts`, `GET /v1/hosts/{id}` (`hosts.py:318,368`) | (none — reads HostStore DB) | host list / status | sidebar host list, online check |
| (session stop / delete) (`sessions.py:7945`) | `host.stop_runner` | stopped | stop a runner |
| `GET /v1/runners/{id}/status` (`runner_tunnel.py:244`) | (none — reads RunnerExitReports) | online + error cause | client polling a never-connecting runner |

**Message format**: all frames are JSON objects with a `kind` discriminator + a `request_id`
(except hello/runner_exited) + a `traceparent` envelope key. Encode/decode in `frames.py`.

**Streaming vs durable**: host control frames are **neither streamed to the client nor
persisted in conversation history** — they are ephemeral control plane. What IS durable is
the *side effect*: `conversation_store.set_runner_id` / `set_host_id` (workspace, host_id,
git_branch persisted on the session row), and `host_store.upsert_on_connect` /
`heartbeat` / `set_offline` (the `hosts` DB table). The `host.list_dir` result is returned
synchronously in the REST response, not persisted.

---

## Q: Disconnects (mid-turn) — host tunnel perspective

Two distinct disconnects matter here:

1. **Host tunnel drops** (host↔server WS closes): the host's reconnect loop
   (`connect.py:1269-1345`) reconnects with backoff. Runner subprocesses **keep running**
   across a host-tunnel blip (they have their own tunnels). On reconnect the host re-sends
   `host.hello` with the still-alive `runners` list → server reconciles. A runner that died
   *during* the blip has its `host.runner_exited` report **parked** in `_unreported_exits`
   and flushed right after the next hello (`:1491-1493`) — so the failure isn't lost.
   On a remote (Apps-fronted) server, an abrupt `no close frame`/`502` is treated as an
   ingress recycle → prompt 0.5s reconnect, specifically so a `host.launch_runner` frame in
   flight isn't dropped ("runner did not connect").
2. **Runner dies mid-turn**: `_watch_runner` (`:898`) detects the unexpected exit (still in
   `self._runners`), composes exit code + log tail, sends `host.runner_exited`; server's
   `on_runner_exited` marks the session failed + pushes the cause to the open view. This is
   the only failure signal when a runner crashes *before* its own tunnel connects.

Server liveness: 3 missed 30s pings → server closes the host 4003; the DB heartbeat
freshness gate (`HOST_LIVENESS_TTL_S`) drops the host's sessions out of the connected set
even if a hard crash skipped `set_offline`.

---

## Q: Forking a session / resuming — host involvement

The host participates in fork/resume through **git worktrees** and **runner binding**:

- **Fork with a new branch**: `POST /hosts/{id}/runners` (or `POST /v1/sessions`) with a
  `git` body → server validates branch name → `host.create_worktree {repo, branch, base}`
  → host runs `git worktree add` in a thread (`connect.py:1194`) → returns
  `worktree_path` → server binds the runner to the **worktree path** as workspace.
  Worktree is created **before** the atomic `set_runner_id` CAS so a lost race or failed
  launch rolls it back via `host.remove_worktree` (no orphan worktree/branch).
- **Resume**: re-binds a previously-unbound clone of the session to a fresh runner
  (`set_runner_id` CAS; second concurrent launch gets 400). The host just spawns a new
  runner for the resolved workspace. *How much transcript loads into the runner* is a
  runner/runtime concern, not the host — the host only provides the cwd and the spawn.
- **Cleanup**: `host.remove_worktree` (opt-in) derives the main repo from the worktree path
  itself and optionally `git branch -D` (`frames.py:393-415`).

---

## Q: Credential resolution — (runner↔server) host-minted token, and host↔server refresh

The host is where the **runner↔server binding credential** is minted and where the
**host↔server bearer** is refreshed:

- **Runner binding token**: server generates `binding_token = secrets.token_urlsafe(32)`,
  derives `runner_id = token_bound_runner_id(binding_token)`, sends it in
  `host.launch_runner`; the host injects it into the runner env
  (`RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR`) so the runner authenticates its own tunnel back
  to the server. One-time, per launch.
- **Host↔server bearer refresh**: `_build_connect_headers` (`connect.py:1409`) re-mints the
  bearer on **every reconnect** (stored `omnigent login` OIDC token first, then ambient
  Databricks creds) — long-lived hosts survive token expiry without a restart. Managed
  sandboxes use the static `X-Omnigent-Host-Token` injected at spawn instead.
- **LLM creds** are NOT resolved by the host as "the host's"; they are *forwarded* from the
  host env to the runner only for the names in `HARNESS_CREDENTIAL_ENV_VARS`
  (`connect.py:352`) — the host owner provisions ANTHROPIC_*/OPENAI_*/etc. precisely so
  their runners can authenticate. Everything else is stripped (allowlist).

---

## Q: Caching / refresh cadence (host-side)

- `configured_harnesses` (PATH probe + config.yaml read) — recomputed **on every
  (re)connect** in `host.hello`, off the event loop (`connect.py:1484`). Not a long-lived
  cache; the authoritative gate is the per-launch `harness_is_configured`.
- `RunnerExitReports` — in-memory TTLCache, **600s TTL**, max 1024 entries
  (`host_registry.py:34-35`); purely a memory bound (runner ids are unique per launch).
- Host bearer token — re-minted every reconnect (no persistent cache held by the daemon;
  the underlying Databricks SDK may cache, `_entry.py` notes a per-call resolve).
- Host registry / `hosts` DB table — in-memory per-replica (`HostRegistry`) + persistent
  DB (`HostStore`); heartbeat refreshes last-seen every 30s ping tick.

---

## Q: Policy / permission hooks at the host layer

**None.** The host daemon does no policy/elicitation work. It is a pure control + FS agent.
Its only *authorization* checks are: pre-accept tunnel ownership (host_id owner match,
managed-token scope) and, server-side, the per-route owner/session authz (`require_user`,
`resolve_host_launch`, owner check on the FS endpoints). Policy enforcement and
elicitation live in the runner/harness/policy-server layers, not here. The host's one
harness-aware decision is refusing to spawn an unconfigured harness
(`HARNESS_NOT_CONFIGURED`), which is a capability check, not a policy.

---

## Q: System resources — how runners (and thus shells) get a working dir

The host resolves the **runner's cwd**: the server sends a `workspace` in
`host.launch_runner` (already realpath-canonicalized via a prior `host.stat` and validated
against the agent's `os_env.cwd` boundary). The host `Path(workspace).expanduser()`, checks
`is_dir()`, and passes it as `RUNNER_WORKSPACE_ENV_VAR` + via the env. The host owns `~`
expansion (only it knows its `$HOME`). Shells the agent later spawns inherit from the
runner, not directly from the host. The host never opens shells itself.

---

## Per-harness differences (claude-sdk / claude-native / codex / codex-native / polly)

At the host-channel layer there is **one launch path for all harnesses**:

- The only harness-aware step is `harness_is_configured(frame.harness)` in `_handle_launch`
  (`connect.py:766`). The server resolves the agent's canonical harness
  (`_resolve_agent_harness`) and stamps it into `host.launch_runner.harness`; the host
  refuses with `HARNESS_NOT_CONFIGURED` when that harness's CLI/cred is missing on the
  machine. `harness=None` (older server / unresolvable) skips the check → fail open.
- `configured_harnesses` in `host.hello` reports per-harness readiness using *every accepted
  spelling* (claude-sdk, codex, etc.) so the server/UI can show which harnesses this machine
  can run before a launch.
- **polly** = a custom agent running on a harness (typically claude-sdk); from the host's
  view it's just whatever canonical harness the agent resolves to — no special casing.
- **native vs sdk** harnesses are identical to the host: both spawn the same
  `omnigent.runner._entry`; the native-vs-SDK distinction is entirely inside the runner.
  (A few native-specific *env vars* ride the allowlist — `OMNIGENT_CLAUDE_LAUNCHER` for
  native-Claude launcher plugins, Bedrock flags — but the spawn mechanism is the same.)
- **codex / codex-native**: no live trace (Databricks AI-gateway creds expired, 403).
  By code, codex launches identically; the host would forward `CODEX_ACCESS_TOKEN` /
  `OPENAI_*` from `HARNESS_CREDENTIAL_ENV_VARS` and gate on `harness_is_configured("codex")`.

---

## Trace evidence (concrete)

Trace `aee57ff3da91a69081625d1804126850` (`trace_tools.py tree`), 38 spans, services
`['omni-host','omni-server']`, `session.id=-` (control plane carries no session id by design):

```
omni-server POST /v1/hosts/{host_id}/runners   ← root, FastAPI auto-instrument
  omni-host   host.stat            ← CONSUMER, parented via traceparent in the frame
  omni-host   host.launch_runner   ← CONSUMER, parented via traceparent in the frame
```

- Jaeger `omni-host` operations = `["host.stat","host.launch_runner"]` exactly.
- Edge construction: server stamps `traceparent` into the frame JSON at
  `encode_host_frame`→`_encode_payload`→`inject_trace_context` (`frames.py:519-520`); host
  re-parses the carrier and opens `consume_frame_span(kind, carrier)` named by the frame
  `kind` (`connect.py:1553`). This proves the manual JSON-frame propagation works across the
  process boundary (the central claim of OBSERVABILITY §6.4).
- The `chat.db` PRAGMA/SELECT/UPDATE/connect spans in the tree are DB noise — filter them.
- (per doc — line drift) OBSERVABILITY §6.4 cites `connect.py:1432` / `host_tunnel.py:312`;
  actual: `_serve_frames` at 1462, `_receive_loop` at 369, consumer span at
  `connect.py:1553`. Mechanism matches.

---

## Failure branches & gaps (channel-focused)

- `HARNESS_NOT_CONFIGURED` (412), workspace-missing (502), launch-timeout (504),
  host-offline (409), connection-replaced (409), version-skew (4002 close), cross-owner
  (409 pre-accept). All covered above + in `architecture/host.md §10`.
- **Exit reports are per-replica, in-memory (TTL 600s)** — on a multi-replica deploy the
  status poll and the report must hit the same replica that holds the host tunnel.
- `host.list_dir` exposes the **whole host FS** to the authenticated owner (not
  session-scoped) — owner authz is the only boundary.
- Runner spawn is **not** part of the user-request trace (no TRACEPARENT in spawn env) →
  runner traces are root-stitched by `session.id` only; a launch→first-turn correlation
  must be done by `session.id`, not parent span.

## Open questions

- Is the "TRACEPARENT in spawn env only if a span is active" branch (OBSERVABILITY §6.4)
  actually wired? Current `_build_runner_env` does not add it. If intended, the launch→runner
  trace gap is by design; if not, it's an unfinished phase-2 item.
- What consumes the **stored** `configured_harnesses` (in the `hosts` DB row) vs. the live
  hello copy — UI capability hints?
- Multi-replica: is there sticky routing guaranteeing the runner-status poll lands on the
  replica holding the host tunnel (so `RunnerExitReports.get_visible` finds the report)?
