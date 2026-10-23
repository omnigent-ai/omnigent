# Session orchestration

An agent in a private session can send work to another session owned by the
same user, then read its status/history. It can also create private top-level
workers on its host or a new managed sandbox. This is behind the disabled-by-default
`session_orchestration` deployment flag.

## Sub-features

- `message`: private sender, same owner and workspace; shared destinations are
  allowed and their readers see the message. Normal queue/steering applies.
- `read-results`: the manager reads history/status; replies are not forwarded.
  Existing owner-authorized reads remain allowed when a session is shared.
- `create`: `sys_session_create` retains child creation by default; `same_host`
  and `managed` placements create independent, initially private sessions.
  Managed creation uses the server's configured default sandbox provider.
- `sharing`: sharing the sender blocks subsequent sends and independent creates.
  Existing work continues. Making it private restores eligibility.
- `scheduled`: newly scheduled runs and their children cannot use the new sends
  or independent creates. Ordinary scheduling, child work and reads stay available.
  An owner can still message a long-lived private manager directly.
- `refusal`: disabled flag, another owner, shared sender or wrong runner binding
  refuses the operation. Unsupported servers reject independent creation without
  falling back to ordinary creation. A delivery timeout is uncertain and must not auto-retry.

## How to get to it (user POV)

- In an SDK session's Chat, ask the agent to send a task using
  `sys_session_message`, then inspect the destination's history.
- In a native session's Chat or agent terminal, ask for the same operation;
  the native MCP relay supplies the tools.
- In a session with creation tools enabled, ask for a worker with
  `sys_session_create` and `placement="same_host"` or `placement="managed"`.
- Use the existing Share dialog to share the sender or receiver, then repeat.

## Driving it with the repro environment

Preconditions: enable `OMNIGENT_FEATURES=session_orchestration` only on the
isolated test server. Own both sessions. Same-host creation requires a connected
owned host/workspace. Managed creation requires a launch-capable provider in the
server's sandbox configuration (for example, Modal or Daytona). Internal deployments
can supply their own provider through the existing launcher interface.

- Own environment: `uv run --no-sync pytest
  tests/e2e/test_private_session_orchestration.py --reruns 0` uses real local
  host, server, SDK and Codex processes with a scripted model. It submits chat
  requests over HTTP and checks send, history reads, same-host creation, and
  sharing refusal. It also checks that unconfigured managed creation gives a clear
  refusal without creating a session. Codex and tmux are required for the native row.
  It also fires an owner-created schedule through the real server/host and checks
  that the resulting worker cannot message a peer or create an independent worker.
- Component integration: `uv run --no-sync pytest
  tests/server/integration/test_session_orchestration.py --reruns 0` checks the
  authenticated routes, ownership stores, refusal matrix and managed request /
  durable startup contract with simulated provisioning. It covers default provider
  selection for OSS and embedding configurations, and an absent orchestration route.
- Browser/terminal manual check: send a distinctive task to a second session;
  open its Chat and confirm the source label and result. Ask the manager to
  read it. Share the manager and repeat: the tool must report refusal and the
  destination must contain no new task. API runs do not prove these UI actions.
- Live sandbox provisioning and native terminal-entered prompts require separate
  verification; local process checks do not cover them.

## Gotchas

- Same host also means the same workspace; separate sessions do not isolate
  filesystem edits. Managed creation provisions a new environment.
- Independent creation uses `/v1/sessions/orchestrated`, which requires a source;
  older servers cannot silently accept the request as ordinary session creation.
- A shared receiver can continue working but cannot message back while shared.
- Privacy checks govern these tools. They do not confine arbitrary code holding
  owner credentials or sanitize shared content the manager reads.
- The source token authenticates its runner, not an exact session within that
  runner. Stable-ID runner pools using allow-listed tunnel tokens are unsupported;
  this gate requires token-derived runner IDs and refuses other bindings.
- Peer messages carry only task text and owner attribution. They do not forward
  per-event model/tool overrides or a caller-supplied delivery ID.
- Already admitted work can finish after sharing changes. Unsharing does not
  erase prior conversation content.
- Scheduled origin is recorded before launch, cannot be edited by clients, and
  survives deletion of the schedule. Sessions predating this marker may have no
  recoverable scheduling origin; their upgrade policy still needs a decision.
- Existing child creation and `sys_session_send` keep their parent/completion
  semantics. Independent workers use `sys_session_message` for later work.
- The server enforces the flag even though tool schemas are discoverable while
  it is disabled. Peer messaging has no additional per-agent opt-in; creation
  retains its existing `spawn` advertisement gate. No schema migration or
  per-owner settings page is needed.
