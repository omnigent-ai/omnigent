# Session creation debug events

The optional debug sink records lifecycle events using the existing `event_name`,
`session_id`, and `attributes` columns. No additional telemetry backend or table
schema is required. Ordinary logs inherit IDs from the active lifecycle scope;
explicit callsite IDs take precedence. The sink remains best effort.

## Correlation

- `attributes.request_id` is the server-generated HTTP request ID returned in
  `X-Request-Id`. It links a create request to its persisted session, including
  requests rejected before a session exists. It is not an end-to-end attempt ID.
- `session_id` links the session across requests and processes.
- `attributes.runner_id` identifies a runner generation. The runner reads its
  existing `OMNIGENT_RUNNER_ID`; the server and host bind known IDs around launch,
  initialization, connection callbacks, and relay tasks. No per-log store lookup.
- `attributes.host_request_id` is the host protocol's launch correlation ID. It
  is deliberately separate from the HTTP request ID.
- Runner session initialization and session-scoped HTTP requests override the
  primary-session fallback, so child-session work retains the child's ID.
  Host-wide logs outside a launch scope do not inherit a session or runner.

Context is copied into asyncio tasks and `asyncio.to_thread` workers. Threads
created by other mechanisms must bind their own context or pass explicit IDs.
The HTTP request ID does not automatically cross the runner tunnel. Join the
server and runner by session and runner IDs, never by request ID alone.

## Events

| Event | Producer | Meaning |
| --- | --- | --- |
| `session_creation_started` | Server | A request entered the create-session route, before validation. |
| `session_created` | Server | Persistence succeeded; links the create request to a session and any existing runner. |
| `session_creation_accepted` | Server | The create HTTP request succeeded. This is not runner readiness. |
| `session_creation_failed` | Server | The create request failed, including 4xx, cancellation, and errors after persistence. |
| `session_runner_bound` | Server | A runner binding was persisted. `operation` is `create`, `launch`, `replace`, or `bind`; none alone defines a new creation. |
| `sandbox_launch_stage` / `sandbox_launch_failed` | Server | Managed provisioning progress; failures preserve the last known stage. Sandbox `ready` only means provisioning/connection finished. |
| `runner_launch_started` / `runner_spawned` | Host | Launch received / subprocess spawned. Neither proves connectivity. |
| `runner_launch_failed` | Server or host | Host refusal, spawn error, or launch acknowledgement failure. Recovery can still succeed. |
| `runner_died` | Host | Unexpected process exit, including before tunnel connection. |
| `runner_connected` | Server or runner | Runner tunnel connection observed. Also emitted on reconnect. |
| `runner_reconnect_callback_failed` | Runner | The reconnect callback failed; not a successful connection milestone. |
| `runner_connect_failed` | Server | Managed runner connection deadline expired. |
| `runner_session_init_started` / `runner_session_initialized` / `runner_session_init_failed` | Server or runner | Initialization request, successful response, or exception/non-success response. Cached server initialization does not emit another outcome. |
| `runner_stream_ready` | Server | The relay received its first heartbeat. Includes both session and runner IDs. |
| `terminal_started` / `terminal_start_failed` | Runner | Native terminal adapter completed / failed. A started terminal does not prove an interactive input prompt. |
| `session_runner_ready` | Server | Initialization, current-connection relay readiness, and usable runner input have all been confirmed. |
| `session_readiness_timeout` | Server | Readiness was not observed within the observer's five-minute budget after initialization; includes the pending stage. The metric still uses the original create-request deadline. |
| `session_readiness_unavailable` | Server | The runner is older or the native provider has no reliable input probe. This is a measurement gap, not a proven creation failure. |
| `session_readiness_observation_failed` | Server | The observer itself failed. This is a measurement gap, not a proven creation failure. |

Request completion includes `creation_kind=top_level|child|unknown`, `host_type`,
and HTTP status. Malformed requests that cannot be classified retain `unknown`.
Lifecycle failures include `stage` and, when available, `error_code`. Query event
types and outcomes rather than equating every ERROR with a failed creation.

## One creation-success metric

Count server-received **top-level create requests that become usable within five
minutes**, divided by eligible create requests. Use these rules:

1. Start with distinct `session_creation_started` request IDs, scoped to the
   deployment/workspace and instrumentation rollout. Left-join completion and
   `session_created` using that request ID. Do not start from successful spawns.
2. Exclude known child creations. Include unclassified/rejected requests in the
   conservative denominator; their missing session ID must not discard them.
3. Follow each request's session ID to its recorded runner bindings. Match
   milestones on **both session and runner ID**, within the create's five-minute
   window and the lifetime of that binding. Do not join all logs sharing either
   ID. A runner can host several sessions, and a session can replace its runner.
4. Require `session_runner_ready` for the same binding. The producer checks
   initialization and a live relay on the same tunnel generation, plus native
   input readiness where applicable. Do not require every intermediate log row
   or substitute `terminal_started`, HTTP 201, or a provisioning `ready` stage.
5. Reduce to one result per create request. Recovery within the deadline can
   succeed despite earlier diagnostic failures. Reconnects/resumes add no create
   request and cannot inflate the denominator. A new HTTP create request is a
   new observation; this contract does not invent client retry deduplication.
6. Evaluate only cohorts older than five minutes plus an ingestion allowance.
   Without readiness by the deadline, classify failure and show the recorded
   failure stage, or `readiness_timeout` when no explicit cause was observed.
   Late readiness does not rewrite the deadline result.

The portable Databricks query is in `session_creation_success.sql`. It keeps
requests without a session in the denominator, follows binding lifetimes, and
suppresses the percentage when an unsuccessful request has an explicit
measurement gap. It does not silently drop unsupported harnesses or mix the
old spawn-based denominator into the new series.

Pre-request CLI failures remain outside the server-received denominator.
Missing logs can resemble startup timeouts; validate delivery and version
coverage before interpreting this best-effort debug metric as reliability.

## Readiness implementation and coverage

With the server debug sink enabled, successful initialization starts a bounded
background observer. It establishes/reuses the relay, waits for its heartbeat,
and polls the runner's read-only `/v1/sessions/{id}/readiness` endpoint once per
second. Creation and user-message handling do not wait for the observer. A
successful event is emitted once per session/runner/tunnel generation. The
observer checks the persisted binding again before emitting, and rejects stale
responses after disconnect, relay replacement, rebind, or deletion. Server
shutdown cancels outstanding observers. No new attempt ID is propagated.

The runner records successful initialization and checks the harness process is
still alive. Native providers additionally supply an optional async `input_ready`
hook over the resolved spawn environment. Probes do not type into terminals or
accept trust/authentication prompts. Session deletion and agent reset invalidate
runner readiness; reconnect probes the existing native process again.

| Provider | Positive evidence |
| --- | --- |
| Non-native | Successful session initialization and a still-running harness process. |
| Claude | Live tmux pane with the existing usable-input-box detector. |
| Codex | Live app-server handshake and a successful read of the current native thread. |
| OpenCode | Live server response for the current native session. |
| Pi | Recent heartbeat from the input poller and a live poller process. Relaunch clears the marker. |
| Qwen | Live pane and the boot event emitted after its input watcher starts. |
| Cursor, Kimi, Kiro, Devin, Antigravity | Live pane and the provider's existing input/footer detector. |
| Goose, Hermes | Unsupported: their current settle heuristics do not establish input readiness. |
| Community native providers | Unsupported unless the provider declares an `input_ready` hook. |

Old runners return no readiness endpoint and produce
`session_readiness_unavailable`. Roll out both server and runners before metric
cutover. TUI detectors remain dependent on the underlying CLI's prompt format;
validate them against deployed CLI versions. Readiness confirms usable input,
not that a subsequent model request will succeed or that the browser has rendered
its first frame.

## Verification

Run the focused suites:

```sh
uv run --no-sync pytest -q tests/test_debug_logging.py \
  tests/server/test_creation_logging.py tests/server/test_runner_session_init.py \
  tests/host/test_connect.py tests/runner/test_app_sessions_native_workflow_init.py \
  tests/server/routes/test_sessions_runner_relay.py
```

With the debug sink configured, create a web session and a CLI session. Follow
`session_creation_started` → `session_created` → `session_runner_bound` using
request ID, then follow launch/init/relay events using session and runner IDs.
A rejected create must retain a start and failure event even without a session.
Try a host with an unconfigured harness: the refusal must include the same
session and runner IDs as its binding. Resume an existing session: there must
be no new creation-start event. Check a child session's logs carry its own
session ID while sharing the parent's runner ID.

Create a supported native session without sending a message. Verify exactly one
`session_runner_ready` appears after the terminal becomes interactive, with the
bound session and runner IDs. Delay or fail native startup and confirm there is
no premature ready event. Reconnect during startup and verify that the previous
connection's probe cannot mark the replacement ready. An older runner or a
Goose/Hermes session must report `session_readiness_unavailable`, not success.

## Migration from message-based dashboards

The host spawn log is now emitted by `_handle_launch_impl`. Before rolling out,
update legacy launch queries to accept `event_name = 'runner_spawned'` as well
as the old `_handle_launch` message predicate. Prefer `attributes['runner_id']`
for the join, with message extraction only for older rows. This preserves the
old spawn-based series during the transition; it does not change its denominator
into server-received creates. Start the new creation series at the deployment
cutover after readiness and delivery coverage have been verified.
