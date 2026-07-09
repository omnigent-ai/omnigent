# Omnigent Server API

Four namespaces: agent management (`/api/agents`), conversations
(`/v1/conversations`), sessions (`/v1/sessions`), and session
resources (`/v1/sessions/{session_id}/resources`).

## Compatibility Reference

| Namespace | Compatible with | Reference implementation |
|---|---|---|
| Agent Management (`/api/agents`) | Omnigent (ours) | No external reference — this is our own API. |
| Conversations (`/v1/conversations`) | Omnigent (ours) | No external reference. |
| Sessions (`/v1/sessions`) | Omnigent (ours) | No external reference. Session-first API for long-running agent interactions. See "Sessions API" section below. |
| Session file resources (`/v1/sessions/{session_id}/resources/files`) | Omnigent (ours) | No external reference. Files are scoped to their owning session. |

---

## Agent Management

### Create Agent

```
POST /api/agents
Content-Type: multipart/form-data

Parts:
  bundle: <tarball>       required — must contain config.yaml with a unique
                          name and optional description. The name becomes
                          the "model" for inference requests.

The server validates the bundle on upload: extracts it to a temporary
directory, parses config.yaml, and runs the spec validator. Name and
description are derived from the spec — no separate form fields.

201 Created
{
  "id": "ag_abc123",
  "object": "agent",
  "name": "my-agent",
  "description": "...",
  "created_at": 1774118382
}

409 Conflict — name already exists
400 Bad Request — invalid bundle (corrupt tarball, missing config.yaml,
    spec validation failure, missing name, path traversal, etc.)
```

### List Agents

```
GET /api/agents

Query parameters:
  limit (integer, optional, default: 20, max: 1000)
    Number of agents to return.

  after (string, optional)
    Cursor for forward pagination. Pass the `last_id` from a previous response
    to get the next page.

  before (string, optional)
    Cursor for backward pagination. Pass the `first_id` from a previous response
    to get the previous page.

  order (string, optional, default: "desc")
    Sort order by `created_at`. Either "asc" or "desc".

200 OK
{
  "object": "list",
  "data": [
    {"id": "ag_abc123", "object": "agent", "name": "my-agent", ...},
    {"id": "ag_def456", "object": "agent", "name": "other-agent", ...}
  ],
  "first_id": "ag_abc123",
  "last_id": "ag_def456",
  "has_more": false
}
```

Items in `data` have the same shape as the create/get response.

### Get Agent

```
GET /api/agents/{id}

200 OK — same shape as create response
404 Not Found
```

### Delete Agent

```
DELETE /api/agents/{id}

200 OK
{"id": "ag_abc123", "object": "agent.deleted", "deleted": true}

404 Not Found
```

Cancels all in-flight responses for this agent before deleting.

---

## Session File Resources

Upload files that can be referenced by `file_id` in `input_image` and `input_file`
content types. Files are immutable once uploaded and are scoped to their owning
session. The legacy global `/v1/files` endpoints are removed; clients must use
the session resource namespace.

### Upload File

```
POST /v1/sessions/{session_id}/resources/files
Content-Type: multipart/form-data

Parts:
  file: <binary>        required

201 Created
{
  "id": "file_abc123",
  "object": "session.resource",
  "type": "file",
  "session_id": "conv_abc123",
  "name": "report.pdf",
  "metadata": {
    "filename": "report.pdf",
    "bytes": 214961,
    "created_at": 1774118382
  }
}

400 Bad Request — missing file
404 Not Found — session not found
```

### List Files

```
GET /v1/sessions/{session_id}/resources/files

Query parameters:
  limit (integer, optional, default: 20, max: 1000)
  after (string, optional)
  before (string, optional)
  order (string, optional, default: "desc")
    Sort order by `created_at`. Either "asc" or "desc".

200 OK
{
  "object": "list",
  "data": [
    {"id": "file_abc123", "object": "session.resource", "type": "file", ...},
    {"id": "file_def456", "object": "session.resource", "type": "file", ...}
  ],
  "first_id": "file_abc123",
  "last_id": "file_def456",
  "has_more": false
}
```

### Get File

```
GET /v1/sessions/{session_id}/resources/files/{id}

200 OK — same shape as upload response
404 Not Found — session or file not found, including files owned by another session
```

### Delete File

```
DELETE /v1/sessions/{session_id}/resources/files/{id}

200 OK
{"id": "file_abc123", "object": "session.resource.deleted", "deleted": true}

404 Not Found
```

### Get File Content

```
GET /v1/sessions/{session_id}/resources/files/{id}/content

200 OK
Content-Type: <original media type>
<binary content>

404 Not Found
```

---

## Conversations

Conversations are created automatically. When a response has no `previous_response_id`,
the server creates a new conversation and assigns the response to it. When a response
has a `previous_response_id` pointing to the **latest** response in a conversation,
it joins that conversation.

When `previous_response_id` points to a **non-latest** response (a fork), the server
creates a new conversation. Items up to and including the fork point are copied into
the new conversation with new response IDs, and the new response is added there.
The original conversation is unchanged. Each conversation is always a linear thread
— no branching. Response IDs are globally unique, so `previous_response_id` is
never ambiguous across conversations.

Clients may optionally pass a conversation ID when creating responses (must be
paired with `previous_response_id`). Conversation APIs are primarily for
**retrieval** — listing past conversations, loading message history, and finding
the latest response ID to continue from.

### List Conversations

```
GET /v1/conversations

Query parameters:
  limit (integer, optional, default: 20, max: 1000)
    Number of conversations to return.

  after (string, optional)
    Cursor for forward pagination. Pass the `last_id` from a previous response.

  before (string, optional)
    Cursor for backward pagination. Pass the `first_id` from a previous response.

  order (string, optional, default: "desc")
    Sort order. Either "asc" or "desc".

  sort_by (string, optional, default: "created_at")
    Column to sort on. Either "created_at" or "updated_at".

200 OK
{
  "object": "list",
  "data": [
    {"id": "conv_abc123", "object": "conversation", "title": null, "created_at": ..., "updated_at": ...},
    {"id": "conv_def456", "object": "conversation", "title": "Weather chat", "created_at": ..., "updated_at": ...}
  ],
  "first_id": "conv_abc123",
  "last_id": "conv_def456",
  "has_more": false
}
```

Results ordered by `sort_by` column descending (newest first) by default.

### Get Conversation

```
GET /v1/conversations/{id}

200 OK
{
  "id": "conv_abc123",
  "object": "conversation",
  "title": null,
  "created_at": 1774118382,
  "updated_at": 1774118400
}

404 Not Found
```

### List Conversation Items

```
GET /v1/conversations/{id}/items

Query parameters:
  limit (integer, optional, default: 20, max: 1000)
  after (string, optional)
  before (string, optional)
  order (string, optional, default: "asc")
    Sort order by position in conversation. Either "asc" (chronological) or "desc".

200 OK
{
  "object": "list",
  "data": [
    {"id": "msg_aaa", "response_id": "resp_001", "type": "message",
     "role": "user", "status": "completed",
     "content": [{"type": "input_text", "text": "What's the weather?"}]},
    {"id": "msg_bbb", "response_id": "resp_001", "model": "my-agent", "type": "message",
     "role": "assistant", "status": "completed",
     "content": [{"type": "output_text", "text": "It's sunny in SF.", "annotations": []}]},
    {"id": "msg_ccc", "response_id": "resp_002", "type": "message",
     "role": "user", "status": "completed",
     "content": [{"type": "input_text", "text": "And tomorrow?"}]},
    {"id": "fc_ddd", "response_id": "resp_002", "model": "my-agent", "type": "function_call",
     "status": "completed", "name": "get_weather",
     "arguments": "{\"location\": \"SF\", \"date\": \"tomorrow\"}", "call_id": "call_001"},
    {"id": "fco_eee", "response_id": "resp_002", "type": "function_call_output",
     "status": "completed",
     "call_id": "call_001", "output": "{\"forecast\": \"rain\", \"high\": 58}"},
    {"id": "msg_fff", "response_id": "resp_002", "model": "my-agent", "type": "message",
     "role": "assistant", "status": "completed",
     "content": [{"type": "output_text", "text": "Rain expected, high of 58°F.", "annotations": []}]}
  ],
  "first_id": "msg_aaa",
  "last_id": "msg_fff",
  "has_more": false
}

404 Not Found — conversation doesn't exist
```

Items include all input and output messages, function calls, and function call
outputs accumulated across all responses in this conversation. Each item carries
a `response_id` linking it to the response that produced it. Model-produced items
(assistant messages, function calls, reasoning) include a `model` field identifying
the agent. User messages and function call outputs do not have `model` — the agent
is always recoverable from `response_id` if needed. To continue a conversation,
pass the `response_id` from the last item as `previous_response_id`.

### Update Conversation

```
PATCH /v1/conversations/{id}
Content-Type: application/json

{"title": "Weather chat"}

200 OK
{
  "id": "conv_abc123",
  "object": "conversation",
  "title": "Weather chat",
  "created_at": 1774118382,
  "updated_at": 1774118400
}

404 Not Found
400 Bad Request — invalid field
```

Currently only `title` (string | null) is updatable.

### Delete Conversation

```
DELETE /v1/conversations/{id}

200 OK
{"id": "conv_abc123", "object": "conversation.deleted", "deleted": true}

404 Not Found
```

Deletes the conversation and all associated tasks. Cancels any
in-flight tasks in the conversation before deleting.

---

## Sessions API (`/v1/sessions`)

A session-first API for long-running agent interactions. The session
is the primary resource. Tasks (responses) are internal
implementation details.

A session is a thin layer on top of a conversation: same backing
store, same items, same live SSE publisher. The session API exposes
four endpoints (create, get, post-event, stream) and a small set of
session-scoped events on the wire (see "Stream Events" below).

### Compatibility Reference

| Namespace | Compatible with | Notes |
|---|---|---|
| Sessions (`/v1/sessions`) | Omnigent (ours) | No external reference. Purpose-built for agent-native workflows: live tail, queued input, interrupt. |

### Session Lifecycle States

A session is always in one of these states:

```
idle -> running -> idle
                -> waiting -> running -> idle
                -> failed
```

- **idle**: No agent loop running. The session is ready to accept
  new events. This is the initial state when no `initial_items` are
  posted on creation, and the terminal state after a turn finishes.
- **running**: An agent loop is actively processing. The session's
  SSE stream is emitting events. New events can still be posted —
  they queue behind the current turn and are consumed at the next
  iteration checkpoint.
- **waiting**: The agent loop has parked the current turn waiting
  for an external signal (background tools, sub-agent completion,
  client tool result). Surfaced as a `session.status` event with
  `status: "waiting"`. The loop resumes (back to `running`) when
  the signal arrives.
- **failed**: An unrecoverable error occurred during processing.
  The session cannot accept new events.

`action_required` is intentionally NOT a session status — it lives at
the task layer (see runtime). The session schema's `status` field is a
`Literal["idle", "running", "waiting", "failed"]` and the route layer
rejects any other value with a 500 (fail loud).

### Session Object

The `SessionResponse` Pydantic model (defined in `server/schemas.py`)
is returned by JSON `POST /v1/sessions`, `GET /v1/sessions/{id}`, and
`PATCH /v1/sessions/{id}`:

```json
{
  "id": "conv_abc123",
  "agent_id": "ag_abc123",
  "status": "running",
  "created_at": 1234567890,
  "runner_id": "runner_abc123",
  "items": [
    {"id": "msg_aaa", "type": "message", "role": "user", "status": "completed",
     "content": [{"type": "input_text", "text": "Plan my trip"}]}
  ]
}
```

Fields:

  id (string, required)
    Unique session identifier; also the underlying conversation ID.
    Prefixed with `conv_`.

  agent_id (string, required)
    Durable identifier of the bound agent (e.g. `"ag_abc123"`). Stable
    across renames of the agent. The session is bound by ID, not name -
    name lookups are not supported.

  agent_name (string, optional)
    Human-readable display name of the bound agent (e.g.
    `"claude-native-ui"`). For switch-created session-scoped clones
    this is the spec's clean name, not the clone row's
    `"… (switch ag_…)"` disambiguation name. Changes when the session
    is switched to a different agent in place
    (`POST /v1/sessions/{id}/switch-agent`), so attached clients can
    refresh their displayed agent label. `null` when the server cannot
    resolve the agent row.

  status (string, required)
    Current lifecycle state: `"idle"`, `"running"`, `"waiting"`, or
    `"failed"`.

  created_at (integer, required)
    Unix timestamp (seconds) when the session was created.

  runner_id (string or null)
    Mutable runner binding for this session. `null` until the client
    binds a registered runner with `PATCH /v1/sessions/{id}`.

  external_session_id (string or null)
    Runtime-native session id this conversation wraps (e.g. Claude
    Code's session uuid for `omnigent claude` sessions). Populated
    by the wrapper bridge from the underlying runtime. `null` for
    regular AP-only conversations. Generic across runtimes — at most
    one external session per conversation.

  model_override (string or null)
    Per-session LLM model override, e.g. `"claude-opus-4-7"`. `null`
    means no override is active and the bound agent's spec model
    applies. Persisted on `conversations.model_override`; set via
    `PATCH /v1/sessions/{id}` (also the path the REPL's `/model`
    command uses) so the web picker and the TUI stay in sync.

  cost_control_mode_override (string or null)
    Per-session cost-control switch: `"on"` activates the spec's
    configured cost-control mode, `"off"` disables cost control for
    this session, `null` defers to the spec default. Persisted on
    `conversations.cost_control_mode_override`; set at create or via
    `PATCH /v1/sessions/{id}` (the web "Cost Optimized" toggle) and
    read by the cost-control advisor pipeline at turn start.

  git_branch (string or null)
    Git branch checked out in the session's worktree, e.g.
    `"feature/login"`. Set only when the session was created with a
    server-created git worktree (the `git` block of create); `null`
    otherwise. A non-null value drives the "delete local branch"
    cleanup option on session delete. See
    `designs/SESSION_GIT_WORKTREE.md`.

  items (array, default `[]`)
    Committed conversation items in chronological order: user
    messages, assistant messages, function calls, function-call
    outputs, reasoning. Each item shape matches the
    `GET /v1/conversations/{id}/items` schema.

  pending_elicitations (array, default `[]`)
    Outstanding `response.elicitation_request` event payloads at
    snapshot build time. Replayed by the client as ApprovalCard blocks
    on cold load, since the live SSE stream has no replay buffer.

  pending_inputs (array, default `[]`)
    Un-consumed web-composer user messages on native-terminal
    (claude-native / codex-native) sessions at snapshot build time,
    each `{pending_id, content}`. Native sessions don't persist a web
    message at POST time (the transcript forwarder is the single
    writer), so the Omnigent server holds these in-memory and replays them
    here — the client re-hydrates the optimistic "queued message"
    bubble so it survives navigation / an SSE rebind. Drained when the
    message round-trips back (the matching `session.input.consumed`
    carries `cleared_pending_id`). Empty for non-native sessions, which
    already carry the message in `items`.

  todos (array, default `[]`)
    Current Claude Code todo list for `omnigent claude` sessions.
    Each item: `{content: string, status: "pending"|"in_progress"|"completed",
    activeForm: string}` where `activeForm` is the gerund form of the
    current activity (e.g. `"Running tests"`). Sourced from the
    server's in-memory todo cache (updated by `external_session_todos`
    events). Empty for non-claude-native sessions or before the first
    turn creates todos.

  terminal_pending (boolean, default `false`)
    `true` while the runner is auto-creating the terminal for a
    terminal-first session (claude-native / codex-native), so the Web UI
    shows a spinner on the Terminal pill instead of a silent greyed-out
    button. Cleared to `false` once the terminal lands or auto-create
    fails; from then on the client relies purely on whether a terminal
    resource exists. Set by two paths: (1) directly by the Omnigent server at
    session creation for host-launched terminal-first sessions, so the
    spinner appears immediately before the runner even starts; (2) by the
    relay when the runner's `session.terminal_pending` events arrive,
    covering non-host-launched sessions. The in-memory cache is read at
    snapshot time so a client connecting mid-spin-up still sees the spinner.

### Create Session

Two request shapes are accepted.

#### Create From Uploaded Agent Bundle

```
POST /v1/sessions
Content-Type: multipart/form-data

metadata: JSON string part, e.g. {"title": "debug auth flow"}
bundle: agent .tar.gz file part
```

Request parts:

  metadata (JSON string, required)
    Session metadata. Shape matches `SessionCreateMetadata`:
    `{title?: string | null, labels?: object, reasoning_effort?: "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | null, workspace?: string | null, terminal_launch_args?: string[] | null}`.
    `terminal_launch_args` carries pass-through CLI args for a native
    terminal wrapper, e.g. `["--permission-mode", "bypassPermissions"]`
    (same field as the JSON create path below). Unknown fields fail with 400.

  bundle (file, required)
    Agent tarball. The server validates it exactly like
    `POST /api/agents`: extract the bundle, load `config.yaml`, and
    require a spec `name`.

201 Created:

```json
{"session_id": "conv_abc123"}
```

The server stores the bundle, then creates the `conversations` row
and the session-scoped `agents` row in one database transaction. The
new agent row has `agents.kind` set to `'session'`,
and `conversations.agent_id` points at that agent. If the database
agent write fails, the conversation row rolls back. If multipart or
bundle parsing fails, no database row is written.

400 Bad Request - invalid metadata or invalid agent bundle
409 Conflict - agent write violates a uniqueness constraint
422 Unprocessable Entity - required multipart part is missing

#### Create From Existing Agent

```
POST /v1/sessions
Content-Type: application/json

{
  "agent_id": "ag_abc123",
  "initial_items": [],
  "title": "debug auth flow",
  "labels": {"env": "test"},
  "host_id": "host_abc123",
  "workspace": "/Users/alice/myrepo",
  "git": {"branch_name": "feature/login", "base_branch": "main"},
  "terminal_launch_args": ["--permission-mode", "bypassPermissions"]
}
```

This preserves the existing sessions API contract for clients that
already uploaded or registered an agent. The response is the full
`SessionResponse` shape with `id` set to the new conversation id.

  host_type (string, "external" | "managed", default "external")
    How the session's host is obtained. `"external"` (the default,
    and the pre-existing behavior): the session runs on
    caller-managed compute — a host registered via `omnigent host`
    (pass `host_id`) or a caller-managed runner (no `host_id`).
    `"managed"`: the SERVER provisions a sandbox host from its
    `sandbox:` config (see `omnigent/server/managed_hosts.py`),
    starts `omnigent host` inside it, binds the session to it, and
    launches the runner there. With `"managed"`, `host_id` and
    `workspace` must NOT be set (422) — the server chooses both.
    Provisioning runs in the BACKGROUND: the create returns
    immediately with `host_id` / `workspace` null, and they appear
    on `GET /v1/sessions/{id}` once the sandbox host registers. A
    message POSTed before the launch settles waits for it (and
    reports the launch failure as 503 if it failed) instead of
    failing with "no runner bound". 400 when the server has no
    `sandbox:` config or the configured provider lacks managed
    support. Deleting a managed session terminates its sandbox
    (best-effort), including a session deleted mid-provision.

  host_id (string or null)
    Host to launch the runner on. When set, `workspace` is required
    and validated against the agent's `os_env.cwd` boundary on that
    host (see `designs/SESSION_WORKSPACE_SELECTION.md`).

  workspace (string or null)
    Absolute path on the host. Required when `host_id` is set. When
    `git` is also set, this is the source repository directory; the
    created worktree becomes the stored workspace.

  git (object or null)
    Optional git worktree options. When present (requires `host_id`),
    the server creates a worktree for a new branch on the host and
    starts the runner in it. Shape: `{branch_name: string,
    base_branch?: string | null}`. `branch_name` is validated against
    git ref-format rules. See `designs/SESSION_GIT_WORKTREE.md`.

  terminal_launch_args (array of strings or null)
    Optional pass-through CLI args for a native terminal wrapper
    (claude / codex), e.g. `["--permission-mode", "bypassPermissions"]`
    (the web UI's permission-mode selector). Set at create time so the
    runner has them on the session row before it auto-launches the
    terminal. The flat-list shape is the security
    boundary — no key for a caller to smuggle launch wiring (bridge
    dir, Omnigent URL, auth), which stay runner-owned. Bounds (count /
    length) are validated server-side; a malformed list returns 400.
    `null` for non-native sessions. Settable later via
    `PATCH /v1/sessions/{id}` (last-write-wins). See
    `designs/NATIVE_RUNNER_SERVER_LAUNCH.md`.

400 Bad Request - invalid terminal_launch_args (count / length bounds)
404 Not Found - no agent with that id
422 Unprocessable Entity - invalid request body

### List Sessions

```
GET /v1/sessions

200 OK — paginated list whose `data` entries match `SessionResponse`
minus `items` and snapshot-only fields.
```

Supports cursor pagination and filters such as `search_query` and
`include_archived`.

The `kind` filter scopes which conversation kinds are listed:
`default` (the default) returns only top-level user-initiated
sessions, `sub_agent` returns only sub-agent child sessions, and
`any` returns both. `any` powers the new-session agent picker's
discovery of agents that are only bound to sub-agent sessions.

When liveness is wired, each list item includes two orthogonal signals
(matching `GET /health` for the same session id and the
`WS /v1/sessions/updates` list item fields):

  runner_online (boolean)
    Strict runner liveness — `true` iff a runner tunnel is currently
    registered. The sole reachability signal (a dead runner on a live
    host reads `false` here, not `true`).

  host_online (boolean | null)
    Whether the session's host tunnel is live (status online and fresh
    within the host liveness TTL). `null` when the session has no host
    binding. Used to distinguish "runner asleep, host alive — send a
    message to relaunch" from "host offline — reconnect/fork"; never
    folded into reachability.

### Get Session (Snapshot)

```
GET /v1/sessions/{session_id}[?include_items=true&include_liveness=true&refresh_state=false]

200 OK — body matches the `SessionResponse` shape above.
404 Not Found — no session with that id
```

Returns the current snapshot: identity, lifecycle status, all
committed items, and any queued (unconsumed) inputs. Combined with the
live stream, this is the reconnect contract — see "Reconnect
Contract" below.

  include_items (query param, boolean, default `true`)
    When `false`, the committed-items read is skipped and `items` is
    `[]` (an empty list, not an absent field). For callers that
    hydrate the transcript through the paginated
    `GET /sessions/{id}/items` endpoint (the web chat surface), the
    snapshot's copy is redundant and the items read is the most
    expensive step of the snapshot build.

  include_liveness (query param, boolean, default `true`)
    When `false`, the runner/host liveness lookup is skipped and
    `runner_online`/`host_online` stay unset. For callers that source
    liveness from the `/health` poll and the live stream (the web
    chat surface), the snapshot's copy is redundant.

  refresh_state (query param, boolean, default `false`)
    When `true`, runner-derived snapshot overlays (for example skills
    and Codex-native model options) are refreshed from the bound runner
    instead of served from AP-process memory. Browser reload/bind
    requests use this so a page refresh pierces stale capability
    caches after a server-side bug fix.

When runner liveness is wired (and not skipped via
`include_liveness=false`), the snapshot includes:

  runner_online (boolean)
    Whether the session's bound runner/host is reachable. This is
    session-scoped (authorized by access to the session), and matches
    `GET /health?session_id=...` for the same id.

### Delete Session

```
DELETE /v1/sessions/{session_id}[?delete_branch=true]

200 OK — {"id": "conv_abc123", "deleted": true}
404 Not Found — no session with that id
403 Forbidden — caller is not the session owner
```

Requires owner-level access. Tears down runner-side resources, session
files, and the conversation row.

  delete_branch (query param, boolean, default `false`)
    Opt-in git cleanup. When `true` and the session has a
    server-created worktree (`git_branch` set), the host removes the
    worktree directory and deletes its branch (`git worktree remove
    --force` then `git branch -D`). Ignored for sessions with no
    worktree. Best-effort: a cleanup failure does not block the
    delete. See `designs/SESSION_GIT_WORKTREE.md`.

### Bind Session Runner

```
PATCH /v1/sessions/{session_id}
Content-Type: application/json

{
  "runner_id": "runner_abc123",
  "title": "debug auth flow",
  "labels": {"env": "test"},
  "reasoning_effort": "high",
  "model_override": "claude-opus-4-7",
  "collaboration_mode": "plan",
  "external_session_id": "a1b2c3d4-1234-5678-9abc-def012345678"
}
```

Request body:

  runner_id (string, optional)
    Runner id that is already registered with the server's tunnel
    registry. Empty strings fail with 400.

  title (string or null, optional)
    Session title update. `null` leaves the title unchanged.

  labels (object, optional)
    Labels to merge into the existing session labels.

  reasoning_effort (string or null, optional)
    Per-session reasoning-effort hint. Accepted metadata values are
    `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`.
    Provider-specific support is validated when a turn executes.
    Clear values follow the existing sessions API semantics.

  model_override (string or null, optional)
    Per-session LLM model identifier the workflow should use on
    subsequent turns instead of the agent spec's `llm.model`,
    e.g. `"claude-opus-4-7"`. The server does not enumerate valid
    models — the executor validates at turn start. Clear aliases
    `"default"`, `"off"`, and `"reset"` remove the override
    (matching the REPL `/model` command); empty / whitespace-only
    strings fail with 400 rather than silently clearing.
    Leaves the column unchanged when omitted or `null`.

  collaboration_mode (string, optional)
    Codex-native collaboration-mode string. `"plan"` enters Plan mode;
    `"default"` returns to Default mode. Only valid for sessions whose
    wrapper label is `codex-native-ui`. Explicit toggles are forwarded
    to the live runner as `plan_mode_change` before the label is
    persisted; if no live runner or loaded Codex bridge can apply the
    change, the request fails and the stored mode is left unchanged.

  cost_control_mode_override (string or null, optional)
    Per-session cost-control switch: `"on"` activates the spec's
    configured cost-control mode, `"off"` disables cost control for
    this session. Explicit `null` clears the override back to the
    spec default; omitting the field leaves the stored value
    unchanged (`"off"` is a real value here, so field presence — not
    a clear alias — is the clear signal). Any other value fails with
    400 `invalid_input`.

  external_session_id (string, optional)
    Runtime-native session id this conversation wraps (e.g. Claude
    Code's session uuid). Idempotent on same-value writes; the server
    rejects attempts to overwrite an already-set different value with
    400 `invalid_input`. Wrapper bridges should write the value once
    when they first observe it from the underlying runtime.

200 OK - body matches the `SessionResponse` shape above, with
`runner_id` set to the newly bound value when `runner_id` was present.

400 Bad Request - runner is not currently registered; `collaboration_mode`
is used on a non-Codex-native session; or `external_session_id` would
overwrite a different existing value
404 Not Found - no session with that id

This is the mutable affinity primitive for Alpha. The same endpoint
serves create-bind, resume-bind, and recover-bind: the client starts a
runner, waits for registration, then PATCHes the session to the new
runner id. The write replaces any previous value in
`conversations.runner_id`; no history table is maintained.

### Codex-specific APIs

Codex-native session routes, including the Codex Goal subresource, are
documented in [codex-API.md](codex-API.md).

### Post Event

```
POST /v1/sessions/{session_id}/events
Content-Type: application/json

{
  "type": "message",
  "data": {
    "role": "user",
    "content": [{"type": "input_text", "text": "What about hotels?"}]
  }
}

Request body matches `SessionEventInput`:

  type (string, required)
    Event/input discriminator. Recognized values:
      - "message"               — a user message
      - "function_call_output"  — a client-side tool result
      - "function_call"         — a queued tool call (rare; mostly
                                  emitted by the runtime, not clients)
      - "reasoning"             — a queued reasoning item
      - "tool_result"           — alias surfaced by some clients;
                                  see `ITEM_TYPE_TO_DATA_CLS` for the
                                  canonical set
      - "interrupt"             — preempt the running loop (see below)
      - "compact"               — explicit context compaction. Forwarded
                                  to the bound runner; for claude-native
                                  sessions the runner injects `/compact`
                                  into the terminal (Claude Code compacts
                                  its own context) and the Omnigent server
                                  skips its own compaction. For in-process
                                  harnesses the runner 204 no-ops and the
                                  Omnigent server runs the compaction itself
                                  (summarises history, persists a
                                  `compaction` item). Payload: `{}`.
      - "stop_session"          — terminate the live session without
                                  deleting the conversation (owner-only;
                                  requires LEVEL_OWNER). Forwarded
                                  harness-agnostically to the bound
                                  runner, which hard-kills the external
                                  process for harnesses that have one
                                  (claude-native kills its tmux pane) and
                                  204s for in-process harnesses; for a
                                  host-launched session the host's runner
                                  subprocess is stopped too, so its tunnel
                                  drops and `runner_online` flips false.
                                  Stop is non-sticky: no persistent marker
                                  is written, so the next message
                                  auto-relaunches a runner on a live host
                                  (there is no separate "resume" event).
                                  The conversation transcript is
                                  preserved. Returns `{queued: false}`.
      - "external_conversation_item"
                                — internal terminal-observed item
                                  envelope; appends/broadcasts without
                                  starting a duplicate task
      - "external_output_text_delta"
                                — internal terminal-observed assistant
                                  text delta; publishes a transient
                                  `response.output_text.delta` SSE event
                                  without persisting an item or starting
                                  a task. Payload: `{delta: string}`.
                                  The final completed message still
                                  arrives through
                                  `external_conversation_item`.
      - "external_session_status"
                                — internal terminal-observed status edge;
                                  publishes a `session.status` event with
                                  data `{status: "running" | "waiting" |
                                  "idle" | "failed"}`
      - "external_session_usage"
                                — internal terminal-observed token-usage
                                  update; persists `context_tokens` /
                                  `context_window` on conversation labels
                                  and publishes a `session.usage` event.
                                  Payload: at least one of
                                  `{context_tokens: int, context_window: int}`
                                  (native harnesses may also send cumulative
                                  `cumulative_cost_usd` / `cumulative_input_tokens`
                                  / `cumulative_output_tokens` / `model`). The
                                  published `session.usage` event additionally
                                  carries `total_cost_usd` (cumulative session
                                  spend, USD) when the session is priced; it is
                                  omitted when unpriced. The same value is seeded
                                  on the session snapshot as `total_cost_usd`
                                  (`null` when unpriced).
      - "external_reasoning_effort_change"
                                — internal terminal-observed thinking-level
                                  update from a native forwarder. Persists
                                  `reasoning_effort` and publishes a
                                  `session.reasoning_effort` event. Payload:
                                  `{reasoning_effort: string | null}`; `null`
                                  clears to the model default.
      - "external_codex_collaboration_mode_change"
                                — internal Codex app-server collaboration-mode
                                  update. Persists the mode kind as label
                                  `omnigent.codex_native.collaboration_mode`.
                                  Payload: `{mode: "default" | "plan"}`.
      - "external_compaction_status"
                                — internal terminal-observed compaction
                                  edge from the claude-native forwarder
                                  (Claude Code's `PreCompact` and
                                  post-compaction `SessionStart
                                  source=compact` hooks). Republishes as
                                  `response.compaction.in_progress` /
                                  `.completed` / `.failed` so the web UI
                                  brackets Claude's own terminal
                                  compaction with its spinner. Payload:
                                  `{status: "in_progress" | "completed" |
                                  "failed"}`.
      - "external_session_todos"
                                — internal terminal-observed todo-list
                                  update from the claude-native forwarder.
                                  Caches the list in memory (used by the
                                  snapshot `todos` field) and publishes a
                                  `session.todos` SSE event. Payload:
                                  `{todos: [{content: str, status:
                                  "pending"|"in_progress"|"completed",
                                  activeForm: string}]}`.
                                  Malformed items are silently dropped
                                  before caching/broadcasting.
    The route validates `type` against the conversation entity's item
    discriminator map plus the documented control/internal event types.
    Unknown values fail loud with 400 — they are NOT silently enqueued.

  data (object, required)
    Type-specific payload. For `"message"`, `{role, content: [...]}`.
    For `"interrupt"`, typically `{}`. The route also validates `data`
    against the item-type's Pydantic data class for non-interrupt
    types (400 on schema mismatch).

202 Accepted
{"queued": true}                            # regular queued item events
{"queued": false}                           # "interrupt" and status/control bypasses
{"queued": false, "item_id": "item_..."}    # "external_conversation_item"
{"queued": true, "pending_id": "pending_..."} # native-terminal "message" (see below)

400 Bad Request — unknown `type`, or `data` fails the per-type schema
404 Not Found — no session with that id
422 Unprocessable Entity — request body fails Pydantic validation
```

**Native-terminal `message` events return `pending_id`.** On
claude-native / codex-native sessions a web-composer `message` is NOT
persisted at POST time (the transcript forwarder is the single writer);
the server records it in an in-memory pending-inputs index and returns
its `pending_id`. The id is what makes the bubble durable across a
rebind: it (a) re-hydrates from the snapshot's `pending_inputs` (the
replayed bubble carries the id) and (b) is dropped by id when the
matching `session.input.consumed` arrives carrying `cleared_pending_id`.
A client *may* adopt the id onto its live optimistic bubble for id-based
dedupe; the first-party web client deliberately does NOT (it keeps a
client temp id for React-key stability and relies on a stable key +
FIFO matching), so adoption is optional, not required.

**Interrupt is dual-path on purpose.** Posting `{"type": "interrupt"}`
is exposed as an event for API uniformity, but it does NOT enter the
queue — the route invokes the loop's `cancel_loop` directly so the
interrupt can preempt items already queued in front of it. On every
user-triggered cancel the server emits BOTH `response.incomplete`
(reason `"user_interrupt"`, from the runtime) AND
`session.interrupted` (from the route). Co-emitting the
Responses-style event lets off-the-shelf parsers close cleanly while
the session-scoped event carries the cancel intent for session-aware
clients. The internal terminal-observed envelopes also bypass the
queue: `external_conversation_item` appends/broadcasts an
already-observed item and returns its stored `item_id`, while
`external_output_text_delta` publishes a transient
`response.output_text.delta` event without persisting. Its `data` is
`{delta: string, message_id?: string, index?: integer, final?: boolean}`:
`delta` is required; the optional `message_id` / `index` / `final` are
set only for terminal-observed live streaming (claude-native), where
they let a client scope an in-flight buffer to one assistant message,
order its chunks, and detect the last one (the authoritative final text
still arrives separately via `external_conversation_item`). They are
omitted for ordinary task streaming, so the published event's wire shape
is unchanged when absent. `external_session_status` publishes a
`session.status` event without
creating a task or item. Runner-hosted native sub-agent sessions also
mirror that status into the parent stream as `session.child_session.updated`
when the child is registered for fan-out. New user-facing event types should
default to the queue.

### Resolve Elicitation (URL-based)

```
POST /v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve
Content-Type: application/json

{"action": "accept"}   # or "decline" / "cancel"; optional "content": {...}

202 Accepted
{"queued": false}

404 Not Found — no session with that id
422 Unprocessable Entity — body fails Pydantic validation (e.g. a bad
    `action` value)
```

Delivers a human approval verdict for an outstanding elicitation (one
published as a `response.elicitation_request` SSE event) to a dedicated,
owner-gated URL. The `elicitation_id` rides in the URL path; the body is
the MCP `ElicitationResult` — `action` plus optional form `content`.
Requires LEVEL_EDIT on the session.

This is the URL-based counterpart to posting an `approval` event to
`POST .../events`: both converge on the same server-side resolver (set
the parked Future, publish `response.elicitation_resolved` to clear the
pending-elicitation badge, and forward the verdict to the bound runner).
Routing the verdict through this resource-scoped URL keeps human
approval on a dedicated path rather than an in-band session event —
which is what policy ASK gates rely on, so the verdict cannot be
conflated with a generic session event. Any value other than
`action: "accept"` denies.

### Fork Session

```
POST /v1/sessions/{source_id}/fork
Content-Type: application/json

{
  "title": "Exploring alternative approach",   // optional
  "up_to_response_id": "resp_abc123"           // optional
}

Request body matches `SessionForkRequest`:

  title (string | null, optional)
    Title for the forked session. When null or omitted, the server
    derives "Fork of <source_title>".

  up_to_response_id (string | null, optional)
    Truncation point for the copied history ("fork from this
    response"). When set, only items up to and including the last
    item of that response are copied; items after it are dropped
    from the fork. A truncated fork into a native target rebuilds
    its transcript from the truncated items instead of resuming the
    source's full native transcript. When null or omitted, the full
    history is copied.

201 Created — body matches `SessionResponse` (status "idle",
  items are the deep-copied items from the source session).

400 Bad Request — source session is a sub-agent session, has
  no agent binding, or up_to_response_id names no response in
  the source session
404 Not Found — no session with that source_id, or the source's
  agent row is missing
```

Creates a new session by deep-copying every item from the source
session. The server also clones the source's agent (new agent ID,
same bundle and config) so the fork can be reconfigured independently.
The forked session is **not** bound to a runner — clients must
`PATCH /v1/sessions/{id}` with `runner_id` before posting events,
the same way they bind a runner after resuming an existing session.

The response is a full `SessionResponse` snapshot of the fork — same
shape as `GET /v1/sessions/{id}` — with status `"idle"` and all
copied items in chronological order. Clients must bind a runner
before opening the fork's SSE stream or posting events.

### Stream Session

```
GET /v1/sessions/{session_id}/stream

200 OK
Content-Type: text/event-stream

event: session.status
data: {"type":"session.status","data":{"status":"running"}}

event: session.input.consumed
data: {"type":"session.input.consumed","data":{"queued_item_id":"...","type":"message", ...}}

event: response.output_item.added
data: {"type":"response.output_item.added","item":{...}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"Hello"}

...

event: response.completed
data: {"type":"response.completed","response":{...}}

event: session.status
data: {"type":"session.status","data":{"status":"idle"}}

data: [DONE]

404 Not Found — no session with that id
```

**Live tail only.** No `starting_after` parameter, no replay of past
events, no sequence numbers exposed on the wire. Reconnecting clients
reconcile via the snapshot endpoint — see "Reconnect Contract".

The stream stays open until the client disconnects or the conversation
is closed; events flow in publish order. Multiple subscribers to the
same session receive the same events. The stream terminates with
`data: [DONE]\n\n`.

### Stream Events

**Single source of truth: [`openapi.json`](../../openapi.json)** at
the repo root. Every SSE event the server emits to clients is
modeled as a typed Pydantic class in
`omnigent/server/schemas.py`; the discriminated union
`ServerStreamEvent` ties them together, and `scripts/dump_openapi.py`
materializes that union into the OpenAPI 3.2 spec under
`components.schemas.ServerStreamEvent` (referenced from the SSE
routes via the `itemSchema` keyword).

The tables below are derived / illustrative — for canonical wire
shapes, field defaults, and schema constraints consult `openapi.json`
or the per-event Pydantic class docstrings.

Two families coexist on the stream:

**Session-scoped (`session.*`)** — wrap the underlying response
stream and surface queue/interrupt semantics.

| Event | Pydantic class | Wire shape (illustrative) |
|---|---|---|
| `session.status` | `SessionStatusEvent` | `{type, conversation_id, status: "running" \| "waiting" \| "idle" \| "failed"}` |
| `session.reasoning_effort` | `SessionReasoningEffortEvent` | `{type, conversation_id, reasoning_effort: string \| null}` |
| `session.collaboration_mode` | `SessionCollaborationModeEvent` | `{type, conversation_id, mode: string}` |
| `session.input.consumed` | `SessionInputConsumedEvent` | `{type, data: {queued_item_id, type, data, position}}` (nested envelope) |
| `session.interrupted` | `SessionInterruptedEvent` | `{type, data: {requested_at, queued_item_id?: null}}` (nested envelope) |
| `session.created` | `SessionCreatedEvent` | `{type, conversation_id: <parent>, child_conversation_id, agent_id, ...}` — emitted on the PARENT session's stream when a sub-agent is spawned. |

> **Note on `session.input.consumed`:** This event name and payload
> may change in a future revision; clients should isolate the
> constant rather than hardcoding it. Importing
> `SessionInputConsumedEvent` from
> `omnigent.server.schemas` is the supported pattern.

**Response (`response.*`)** — emitted by the executor (and the AP
streaming routes for the lifecycle events). The session stream
multiplexes them; the per-response stream emits them directly.

| Event | Pydantic class |
|---|---|
| `response.created` | `CreatedEvent` |
| `response.queued` | `QueuedEvent` |
| `response.in_progress` | `InProgressEvent` |
| `response.completed` | `CompletedEvent` |
| `response.failed` | `FailedEvent` |
| `response.incomplete` | `IncompleteEvent` |
| `response.cancelled` | `CancelledEvent` |
| `response.output_text.delta` | `OutputTextDeltaEvent` |
| `response.output_item.done` | `OutputItemDoneEvent` |
| `response.output_file.done` | `OutputFileDoneEvent` |
| `response.reasoning.started` | `ReasoningStartedEvent` |
| `response.reasoning_text.delta` | `ReasoningTextDeltaEvent` |
| `response.reasoning_summary_text.delta` | `ReasoningSummaryTextDeltaEvent` |
| `response.retry` | `RetryEvent` |
| `response.error` | `ErrorEvent` |
| `response.compaction.in_progress` | `CompactionInProgressEvent` |
| `response.client_task.cancel` | `ClientTaskCancelEvent` |
| `response.heartbeat` | `HeartbeatEvent` |
| `response.elicitation_request` | `ElicitationRequestEvent` |

See the per-class docstring in `omnigent/server/schemas.py` for
the canonical wire shape and field types of each `response.*` event.
When a child/sub-agent elicitation is mirrored into an ancestor stream,
`response.elicitation_request.params.target_session_id` is the child
session whose resolve endpoint must receive the verdict.

### Reconnect Contract

The session API has **no replay machinery**. To reconnect to a
session after a disconnect:

1. **Open the SSE stream** (`GET /v1/sessions/{id}/stream`). The
   stream is registered eagerly at session create and survives
   across turns, so subscribing is safe at any point in the session
   lifecycle — including before the first turn starts.
2. **GET the snapshot** (`GET /v1/sessions/{id}`).
3. **Dedupe items between the snapshot and the stream by item id.**
   Items in `snapshot.items` that also appear in stream events are
   the same item — drop the duplicate. Server-issued IDs are stable.

Opening the stream BEFORE the snapshot is still recommended so no
events fire in the gap, but because the stream queue stays alive
across turns, transient races during reconnect are bounded to the
in-flight HTTP roundtrip rather than the full turn duration. Clients
should rely on `session.input.consumed` events and item-id dedupe
against the snapshot to reconcile accepted inputs.

### Sessions Typical Flow

```
1. Client creates a session with an uploaded agent bundle
   -> POST /v1/sessions multipart {metadata, bundle}
   -> 201 {"session_id": "conv_abc123"}

2. Client binds the registered runner
   -> PATCH /v1/sessions/{id} {"runner_id":"runner_abc123"}
   -> 200 session snapshot with runner_id set

3. Client opens the SSE stream
   -> GET /v1/sessions/{id}/stream
   -> events flow: session.status -> session.input.consumed
      -> response.* deltas -> response.completed -> session.status (idle)

4. Client sends a user message
   -> POST /v1/sessions/{id}/events {type:"message", data:{...}}
   -> 202 {"queued": true}
   -> existing SSE stream emits events for the turn

5. Client loses connection (laptop close, network flap)
   -> agent continues processing server-side

6. Client reconnects:
   a. Open new GET /v1/sessions/{id}/stream  (subscribe FIRST)
   b. GET /v1/sessions/{id}                  (snapshot SECOND)
   c. Dedupe items between snapshot and stream by item id

7. Client interrupts a running agent
   -> POST /v1/sessions/{id}/events {type:"interrupt"}
   -> 202 {"queued": false}
   -> stream emits both response.incomplete (reason:user_interrupt)
      and session.interrupted, then session.status:idle
```

---

## Not Yet

- `PUT /api/agents/{id}` — update agent (new bundle)
- Stream resumption on GET (sequence_number-based reconnection)
- Authentication
- Rate limiting
- User filtering on conversation list (by metadata, dedicated user field, or auth identity)
- Conversation update metadata (beyond title)
- Search across conversations (full-text search over message content)
- Multi-user identity (`user` field on requests/items to attribute messages in shared conversations)
- `logprobs` on `output_text` content blocks (optional, used with `top_logprobs`)
- `metadata` on sessions / tasks — caller-attached key-value pairs (max 16 keys, keys ≤64 chars,
  values ≤512 chars).
- `purpose` field on file uploads (e.g. `"input"`, `"fine-tune"`)
- Audio input (`input_audio` content type)
- Additional output item types: `image_generation_call`, `web_search_call`, `file_search_call`,
  `code_interpreter_call`, `mcp_tool_call`, `computer_call`, `local_shell_call`, `apply_patch_call`,
  `compaction`
