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
| `runner_connect_failed` | Server | Managed runner connection deadline expired. |
| `runner_session_init_started` / `runner_session_initialized` / `runner_session_init_failed` | Server or runner | Initialization request, successful response, or exception/non-success response. Cached server initialization does not emit another outcome. |
| `runner_stream_ready` | Server | The relay received its first heartbeat. Includes both session and runner IDs. |
| `terminal_started` / `terminal_start_failed` | Runner | Native terminal adapter completed / failed. A started terminal does not prove an interactive input prompt. |

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
4. Require initialization success and relay readiness for the same binding.
   Native sessions additionally require a genuine interactive-readiness signal.
   Do not substitute `terminal_started`, HTTP 201, or a provisioning `ready` stage.
5. Reduce to one result per create request. Recovery within the deadline can
   succeed despite earlier diagnostic failures. Reconnects/resumes add no create
   request and cannot inflate the denominator. A new HTTP create request is a
   new observation; this contract does not invent client retry deduplication.
6. Evaluate only cohorts older than five minutes plus an ingestion allowance.
   Without readiness by the deadline, classify failure and show the recorded
   failure stage, or `readiness_timeout` when no explicit cause was observed.
   Late readiness does not rewrite the deadline result.

This change supplies the shared correlation and lifecycle milestones. This OSS
revision does **not** provide a universal `terminal_interactive` event or emit a
synthetic `session_creation_ready`. Deployments must supply the genuine native
readiness signal before switching a dashboard to the usable-session metric.
Pre-request CLI failures remain outside the server-received denominator.
Missing logs can resemble startup timeouts; validate delivery and version
coverage before interpreting this best-effort debug metric as reliability.

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
