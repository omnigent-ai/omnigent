# Session orchestration

An agent in a private session can send work to another session owned by the
same user, then read its status/history. It can also create private top-level
workers on its host or a new managed sandbox. This is enabled by default, with
`-session_orchestration` in `OMNIGENT_FEATURES` as a server kill switch.

## Sub-features

- `message`: private sender, same owner and workspace; shared destinations are
  allowed and their readers see the message. Normal queue/steering applies.
- `read-results`: the manager reads history/status; replies are not forwarded.
  Existing owner-authorized reads remain allowed when a session is shared.
- `created-by`: independent workers record their creating session. The worker's
  Chat/Terminal header rechecks access when opened, hides attribution during the
  lookup, and links to the creator when accessible; otherwise it says "Created by
  another session". `sys_session_list` and `sys_session_get_info` expose
  `created_by_session_id` so managers can identify their workers across restarts;
  `sys_session_list` with `include_archived` also lists archived workers, each
  marked `archived`.
- `kill-switch`: an explicit disable refuses new peer sends and independent
  creates, even if an enable is also listed, and hides the Automations page's
  trigger templates (`/v1/info` reports `features.session_orchestration`). Restart every server replica to
  apply it. Already accepted tasks may finish; ordinary reads and child work remain.
- `create`: `sys_session_create` retains child creation by default; `same_host`
  and `managed` placements create independent, initially private sessions.
  Managed creation uses the server's configured default sandbox provider.
- `sharing`: sharing the sender blocks subsequent sends and independent creates.
  Side chats and their children also inherit restrictions from the source chain.
  Accepted work, including tasks saved while a worker starts, continues. Making
  the sender private restores eligibility.
- `scheduled`: scheduled runs, their forks and children use the new sends and
  independent creates like any private session their owner owns, so an
  automation can message the owner's other sessions or start workers. The
  Automations page's Jira/Slack/GitHub trigger templates rely on this: their
  kept session starts one worker per match (`docs/AUTOMATIONS.md`).
- `refusal`: disabled flag, another owner, shared sender or wrong runner binding
  refuses the operation. Unsupported servers reject independent creation without
  falling back to ordinary creation. Timeouts, 408/429/5xx responses and invalid
  acknowledgements are uncertain and must not auto-retry. After uncertain
  creation, inspect existing sessions before creating another worker.

## How to get to it (user POV)

- In an SDK session's Chat, ask the agent to send a task using
  `sys_session_message`, then inspect the destination's history.
- In a native session's Chat or agent terminal, ask for the same operation;
  the native MCP relay supplies the tools.
- In a session with creation tools enabled, ask for a worker with
  `sys_session_create` and `placement="same_host"` or `placement="managed"`.
  Pass `sandbox_provider`, `sandbox_profile_id`, and `sandbox_profile_revision`
  to choose an authorized profile/revision from the
  [provider catalog](../docs/extending/provider_profiles.md).
- Use the existing Share dialog to share the sender or receiver, then repeat.
- Open an independent worker in Chat or Terminal and follow its "Created by"
  link. On mobile web, the same link is in the header. Ask the manager to list
  its sessions and match `created_by_session_id` to its own session ID.
- On Automations, pick the *Jira tickets*, *Slack messages*, or *GitHub issues*
  chip, or choose **New automation → Create from template** to browse the same
  templates at `/tasks/templates`. Pick a template, fill in the trigger builder,
  and save: the hourly automation's kept session starts one worker per new match
  with `sys_session_create`. Covered by
  `tests/e2e_ui/scheduled/test_scheduled_tasks_page.py::test_scheduled_task_trigger_template_creates_hourly_manager_task`.

## Driving it with the repro environment

Preconditions: use an isolated test server with the default feature settings.
Own both sessions. Same-host creation requires a connected
owned host/workspace. Managed creation requires a launch-capable provider in the
server's sandbox configuration (for example, Modal or Daytona). Internal deployments
can supply their own provider through the existing launcher interface.

For `created-by`, extend the creation journey below: open the worker, reload,
and follow its creator link. Share only the worker with a second viewer and
verify they see "Created by another session" with no creator link or title.
Also revoke creator access after that viewer has visited the creator, then open
the worker through in-app navigation: no cached creator title or link should appear.
Fork the worker and verify the fork has no "Created by" label. The API/runner
contract is covered by `tests/server/integration/test_session_orchestration.py`;
the header states are covered by `web/src/shell/ChatHeader.test.tsx`.

- Own environment: `uv run --no-sync pytest
  tests/e2e/test_private_session_orchestration.py --reruns 0` uses real local
  host, server, SDK and Codex processes with a scripted model. It submits chat
  requests over HTTP and checks send, history reads, same-host creation, and
  sharing refusal. It also checks that unconfigured managed creation gives a clear
  refusal without creating a session. Codex and tmux are required for the native row.
  Both harnesses also exercise a server restart with the explicit kill switch:
  existing runners remain opted in, but their subsequent peer sends and independent
  creates are refused without writes; session-info reads still work.
  It also fires an owner-created schedule through the real server/host and checks
  that the resulting worker cannot message a peer or create an independent worker.
- Component integration: `uv run --no-sync pytest
  tests/server/integration/test_session_orchestration.py --reruns 0` checks the
  authenticated routes, ownership stores, refusal matrix and managed request /
  durable startup contract with simulated provisioning. It covers default provider
  selection for OSS and embedding configurations. Runner tests cover an absent
  orchestration route without falling back to ordinary creation.
  Two ASGI replicas sharing a database verify host routing for root, child and
  side-chat destinations with the production runner client/auth and a simulated
  ingress. Same-host creation covers root/child managers, inherited host routing,
  legacy keyless routing and refusal before writes on the wrong replica. A tunnel
  loss after workspace validation leaves one discoverable worker and reports
  uncertainty without retrying creation. Queue tests check refusal before saving
  and replay after sharing. Scheduled-origin tests cover sends and
  creates from a scheduled run, its full fork after schedule-history deletion,
  and its children.
- Browser/terminal manual check: send a distinctive task to a second session;
  open its Chat and confirm the source label and result. Ask the manager to
  read it. Share the manager and repeat: the tool must report refusal and the
  destination must contain no new task. API runs do not prove these UI actions.
  Append `-session_orchestration` to the isolated server's feature settings and
  restart it, then repeat from a private manager: expect a disabled error, no new
  task and no independent worker. Read history/info to confirm reads still work.
- Live sandbox provisioning and native terminal-entered prompts require separate
  verification; local process checks do not cover them.

## Gotchas

- "Created by" records origin only: workers remain top-level and have their own
  permissions and lifecycle. Sharing a worker does not share its creator; the
  opaque creator ID remains in worker metadata, but the UI resolves its title
  through a separate authorized read. Forks drop the creator label. Older
  workers have no label; there is no backfill or automatic completion reply.

- Same host also means the same workspace, inherited together from the host-bound
  ancestor for child managers. Separate sessions do not isolate filesystem edits.
  Managed creation reserves its routing host ID when supported by the server.
  If the same-host worker is created but its first task is not confirmed, the
  result includes a top-level error and the created session ID. Inspect that
  session before continuing; do not automatically create a replacement or resend.
  A policy-denied first task also returns an error with the policy reason, while
  retaining the worker ID. Message acknowledgements require a boolean `queued`;
  `queued: false` alone means synchronous handling, not denial.
- Unconfirmed creation includes the requested agent ID/title and any reserved
  managed host ID. Use `sys_session_list`'s `sessions` results to discover candidate
  session IDs, then `sys_session_get_info` to inspect their metadata. A host ID is
  not a session ID; these hints do not prove creation or uniquely identify a worker.
  Same-host `wrong_replica`/409 responses can occur before persistence or during
  launch afterward, so the runner treats them as uncertain and never auto-retries.
  An early wrong-replica refusal creates no row; a late launch failure may leave
  one that the owner can inspect. Keyless legacy routing must be configured on
  the runner; independent creation has no automatic routing fallback.
- Independent creation uses `/v1/sessions/orchestrated`, which requires a source;
  older servers cannot silently accept the request as ordinary session creation.
- A shared receiver can continue working but cannot message back while shared.
- Privacy checks govern these tools. They do not confine arbitrary code holding
  owner credentials or sanitize shared content the manager reads.
- The source token authenticates its runner, not an exact session within that
  runner. Runners are trusted for every session they host: another private owned
  session on the same runner can be named as the source. Stable-ID runner pools
  using allow-listed tunnel tokens are unsupported;
  this gate requires token-derived runner IDs and refuses other bindings.
- Side chats must be private and have a private, owned, available source chain
  to send work or create independent workers. Nested side chats and their children
  inherit this restriction. Missing or cyclic source chains are refused. Ordinary
  reads and incoming work remain available; making the source private restores
  eligibility without erasing copied history.
- Peer messages carry only task text and owner attribution. They do not forward
  per-event model/tool overrides or a caller-supplied delivery ID.
  The textual source prefix is a display hint, not an authorization credential
  or authenticated provenance. Other users can copy it into ordinary messages.
  V1 deliberately keeps this advisory text instead of adding structured provenance.
- Admission is the final source check before delivery or saving a startup task.
  A queued result means acceptance, not execution; destination policy may still
  deny the task during startup replay. Successfully queued tasks can finish after
  the sender is shared; replay retains
  normal destination permissions and policy checks. The check and subsequent
  dispatch/save are not one atomic transaction. Unsharing does not erase history.
- For peer messages, an explicit wrong-replica refusal permits a bounded re-address.
  If the subsequent host lookup fails, the task was not delivered. A successful
  lookup with no host may still attempt legacy keyless delivery; uncertainty on
  that POST remains uncertain. Timeouts,
  other refusals and unparseable delivery acknowledgements are never retried.
- Scheduled origin is recorded before launch, cannot be edited by clients, and
  survives deletion of the schedule and full forks. It no longer limits
  orchestration: an unattended run messages and creates sessions under the same
  owner, privacy and sharing checks as an interactive turn. The release flag
  still turns all orchestration off.
- Existing child creation and `sys_session_send` keep their parent/completion
  semantics. Independent workers use `sys_session_message` for later work.
- The server enforces the flag even though tool schemas are discoverable while
  it is disabled. Peer messaging has no additional per-agent opt-in; creation
  retains its existing `spawn` advertisement gate. No schema migration or
  per-owner settings page is needed.
  V1 treats eligible private agents as delegates across the owner's sessions.
  Unlike sharing, messaging does not grant another user access, but untrusted
  content can still influence an agent to steer other owned sessions. Enabling
  the flag accepts that broader trust; hiding a tool is not authorization.
