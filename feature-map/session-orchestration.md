# Session orchestration

An agent in a session that is private or shared read-only can send work to
another session owned by the same user, then read its status/history. It can also create private sibling
workers grouped in the left sidebar on its host or a new managed sandbox by
default. Set `OMNIGENT_FEATURES=-sibling_sessions` for standalone workers instead.
Peer messaging is enabled
by default; `-session_orchestration` is its server kill switch and also blocks
MCP independent creation.

## Sub-features

- `message`: sender without edit-level sharing, same owner and workspace; shared destinations are
  allowed and their readers see the message. Normal queue/steering applies.
- `sender-attribution`: a delivered message starts with `[Message from session <id>]`.
  The destination's Chat drops that line from the user bubble and shows "From
  @<sender>" as a session pill that opens the sender; the agent still receives
  the prefix. The title comes from sessions the sidebar, the viewed session's
  subagents or the session cache already hold, else one lookup; an unreadable
  sender reads "From another session". The prefix is a label, not proof of origin.
- `read-results`: the manager reads history/status; replies are not forwarded.
  Existing owner-authorized reads remain allowed when a session is shared.
- `created-by`: independent workers record their creating session. The worker's
  Chat/Terminal header rechecks access when opened, hides attribution during the
  lookup, and links to the creator when accessible; otherwise it says "Created by
  another session". `sys_session_list` and `sys_session_get_info` expose
  `created_by_session_id` so managers can identify their workers across restarts;
  `sys_session_list` with `include_archived` also lists archived workers, each
  marked `archived`.
- `list-scope`: `sys_session_list`'s global `sessions` view lists only the
  caller's own sessions by default, like the sidebar's My view. Sessions other
  users shared with the caller appear only with `visibility: "shared"` (or
  `"all"` for both), and those rows carry `owner`. `include_archived` applies to
  every scope. Runner coverage: `tests/runner/test_discovery_list_pagination.py`;
  the route's scopes: `tests/server/routes/test_sessions_shared_projects.py`.
- `kill-switch`: an explicit disable refuses new peer sends and independent
  creates, even if an enable is also listed, and hides the Automations page's
  trigger templates (`/v1/info` reports `features.session_orchestration`). Restart every server replica to
  apply it. Already accepted tasks may finish; ordinary reads and child work remain.
- `messaging-control`: Settings → Sharing → Agent messaging controls new peer
  sends immediately and persists across restarts. `-peer_session_messaging` in
  the server's `OMNIGENT_FEATURES` also refuses them and locks the admin switch;
  in the host/runner environment it hides the tool and refuses calls locally.
  Neither side can override a disable on the other. Child messaging and already
  accepted work continue; session creation keeps its separate orchestration gate.
- `create`: `sys_session_create` retains child creation by default; `same_host`
  and `managed` placements create independent, initially private sessions.
  An optional `permission_mode` starts the session in a mode of its harness;
  modes that skip approval are refused, and on a server that doesn't advertise
  `session_create_permission_modes` the tool refuses the call before creating anything.
  With `sibling_sessions` enabled they share one sidebar family and do not appear
  as child agents; with it disabled they remain standalone.
  Managed creation uses the server's configured default sandbox provider. A
  sibling stays in its creator's project, and an unavailable source project is
  rejected before creation; a different requested project makes a standalone
  session in that project. Without a requested project, a standalone session is
  filed in its creator's project, if that project still exists. A `project_name`
  argument files the session in the owner's project with that name instead,
  created if missing. On an older server that has `sibling_sessions` on and no
  `sibling_other_project_standalone` in `/v1/info`, the tool refuses
  `project_name`. Project defaults do not override the worker's agent or placement.
- `destinations`: Settings → Admin → General → **Nested agent destinations** limits new
  agents to the current host, the current host or a fresh managed sandbox, or
  any available host. The server enforces the same choice for the composer,
  uploaded custom agents, direct API calls, MCP, later host launches, and runner reassignment. Failed
  relaunches retain the prior host so restricted sessions can retry. Any available host is the
  behavior-preserving default. For a standalone session in another project, the policy
  applies only at creation; later host launches and runner reassignment do not check it.
- `sharing`: sharing the sender with edit (or higher) access, including public
  edit, blocks subsequent sends and independent creates. Read-only grants,
  including public read and default-public sessions, do not: viewers cannot add
  input, so they cannot steer the owner's other sessions through the sender.
  Side chats and their children also inherit restrictions from the source chain.
  Accepted work, including tasks saved while a worker starts, continues.
  Downgrading or revoking every edit grant restores eligibility.
- `scheduled`: scheduled runs, their forks and children use the new sends and
  independent creates like any private session their owner owns, so an
  automation can message the owner's other sessions or start workers. The
  Automations page's Jira/Slack/GitHub trigger templates rely on this: their
  kept session starts one worker per match (`docs/AUTOMATIONS.md`).
- `projects`: `sys_project_list` pages the owner's projects oldest first and
  their own sessions with each one's project, marking the calling session; a
  legacy label folder shows only as a session's project. It also forwards the
  sidebar's search (titles and transcripts), its pinned filter and its
  include-archived toggle, and marks pinned and archived rows.
  `sys_session_update` files one of those sessions into a project by name,
  creating a missing project only once the session can move, or unfiles it
  with `""`, through the sidebar's move request; it also renames, pins or
  unpins, and archives or restores the session, all in one PATCH; agents call
  it once per session. An open sidebar regroups without a
  reload; a collapsed folder stays collapsed. Filing, renaming and pinning
  change only how the session is listed, so running sessions, including the
  caller, keep their turn. Archiving stops the target and its running
  sub-agents, as the sidebar's Archive does, so the server refuses an
  agent-tool archive of any session the caller runs under (its parent chain
  or a side chat's source), and the tool also refuses another session on the
  caller's runner. A session may archive itself, for example an ephemeral
  check that is done: the server keeps its runner until that turn ends (at
  most an hour), and an Undo before then keeps it running. The wait is held in
  that server's memory, so a server restart during it leaves the runner up
  until its idle timeout. An archive cannot also file the session into a project that
  does not exist yet, so a refused archive never leaves a new empty project. `sys_project_update` renames a project or sets or clears
  its emoji icon, keeping its other stored defaults; like the sidebar's
  rename it re-files sessions that still belong through the legacy label,
  promotes a label-only folder to a project row, and reports any session it
  could not move. Agents cannot delete projects: the sessions list hides side
  chats, so only the server could tell that a project is empty, and it has no
  check for that yet. Sub-agents follow their parent and a
  family follows its representative for filing; each refusal names the
  session to update instead. An edit-shared caller cannot change a project or
  create or rename one, or rename, pin or archive a session, while
  listing stays an ordinary owner read. A session shared with the caller
  but owned by someone else is refused, even when it is already in the target
  project. Project-only updates refuse to run against a server that does not
  advertise `session_move_source_checks` in `/v1/info`; every other write
  needs `session_organize_source_checks`. `tool_groups.projects: false`
  drops all three tools. Covered by
  `tests/server/integration/test_session_orchestration_projects.py::test_project_tools_list_and_file_running_sessions`,
  `::test_organizing_tools_rename_pin_archive_and_manage_projects`,
  `::test_organizing_tools_refuse_a_source_shared_with_edit_access`,
  `::test_a_child_cannot_archive_its_rebound_parent`
  and the browser journeys in
  `tests/e2e_ui/sessions/test_agent_files_sessions_into_projects.py`.
- `refusal`: disabled orchestration, another owner, edit-shared sender or wrong runner binding
  refuses the operation. Unsupported servers reject independent creation without
  falling back to ordinary creation. Timeouts, 408/429/5xx responses and invalid
  acknowledgements are uncertain and must not auto-retry. After uncertain
  creation, inspect existing sessions before creating another worker.

## How to get to it (user POV)

- As an admin on desktop or mobile web, open Settings → Sharing → Agent
  messaging, or `/settings/sharing`, and toggle **Message other sessions**.
- In an SDK session's Chat, ask the agent to send a task using
  `sys_session_message`, then inspect the destination's history. Its new user
  bubble shows "From @<sender>"; click the pill to open the sender.
- In a native session's Chat or agent terminal, ask for the same operation;
  the native MCP relay supplies the tools.
- In a session with creation tools enabled, ask for a worker with
  `sys_session_create` and `placement="same_host"` or `placement="managed"`.
  Pass `sandbox_provider`, `sandbox_profile_id`, and `sandbox_profile_revision`
  to choose an authorized profile/revision from the
  [provider catalog](../docs/extending/provider_profiles.md).
- In a session's Chat, ask the agent to list your projects and file some
  sessions, including itself, into an existing or new project. The sidebar folders
  update while the agent keeps running.
- Ask the same agent to rename, pin or archive a session, find sessions by a
  word in their title or transcript, or rename a project or give it an emoji;
  the sidebar reflects each change live.
- In a native Claude Code session's Chat or agent terminal, ask the same; the
  relay exposes `mcp__omnigent__sys_project_list`,
  `mcp__omnigent__sys_session_update` and `mcp__omnigent__sys_project_update`,
  and the session lands in its folder
  mid-turn (`test_native_claude_files_itself_into_a_project`, nightly). Cursor
  and Antigravity sessions reach the same three tools through their relay
  allowlists, which approve them without an in-terminal prompt.
- Use the existing Share dialog to share the sender or receiver, then repeat.
  A read-only share of the sender keeps both tools working; an edit share
  refuses them.
- As an admin, open Settings → Admin → General and change **Nested agent destinations**.
  Open **Add nested agent** from a session and inspect its host picker, then try the
  same disallowed destination through the API or MCP.
- **Add nested agent** starts in the source checkout even when the project or user
  defaults request new worktrees. Directory and worktree controls remain editable;
  switching away and back restores the source directory. A manually created worktree
  defaults to the selected checkout's actual branch, including `master`, with an
  editable base branch. Browser
  coverage: `tests/browser_ui/chat/test_nested_agent_workspace.py` (desktop/mobile,
  mocked backend); default and explicit worktree payloads:
  `web/src/shell/NewChatDialog.flow.test.tsx`.
- Open an independent worker in Chat or Terminal and follow its "Created by"
  link. On mobile web, the same link is in the header. Ask the manager to list
  its sessions and match `created_by_session_id` to its own session ID.
- On Automations, pick a _Jira tickets_, _Slack messages_, or _GitHub issues_
  card from suggestions on **Your automations** or the **Templates** tab at
  `/tasks?view=templates`. Suggestions appear below saved automations or the
  empty-state message with All selected and search cleared. The old
  `/tasks/templates` URL redirects to the Templates tab.
  Pick a template, fill in the trigger builder, and save: the polling automation's kept session starts one worker per new match
  with `sys_session_create`. Covered by
  `tests/e2e_ui/scheduled/test_scheduled_tasks_page.py::test_scheduled_task_trigger_template_creates_polling_manager_task`.

## Driving it with the repro environment

Preconditions: use an isolated test server with default settings for nested
creation and peer messaging; set `OMNIGENT_FEATURES=-sibling_sessions` for standalone creation.
Own both sessions. Same-host creation requires a connected
owned host/workspace. Managed creation requires a launch-capable provider in the
server's sandbox configuration (for example, Modal or Daytona). Other deployments
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
  and its children. Off/on/off request-scoped flag changes verify that MCP and
  the orchestration endpoint create private standalone roots when nesting is off
  and siblings when on; explicit sibling requests still refuse while off.
- Browser/terminal manual check: send a distinctive task to a second session;
  open its Chat and confirm the source label and result. Ask the manager to
  read it. Share the manager read-only and repeat: the task must still arrive.
  Share it with edit access and repeat: the tool must report refusal and the
  destination must contain no new task. API runs do not prove these UI actions.
  Append `-session_orchestration` to the isolated server's feature settings and
  restart it, then repeat from a private manager: expect a disabled error, no new
  task and no independent worker. Read history/info to confirm reads still work.
- For `messaging-control`, turn the admin switch off, reload, and confirm a new
  peer send is refused. Turn it back on and confirm delivery resumes. Check that
  child work and a task accepted before disabling still finish. With the server
  flag disabled, the switch stays off and unavailable; with only the runner flag
  disabled, a new SDK/native session omits `sys_session_message`. Restart the
  affected server or host/runner after environment changes. Component coverage is
  in `web/src/pages/SharingPage.test.tsx`, `tests/tools/test_manager.py`,
  `tests/runner/test_session_orchestration.py`, and
  `tests/server/integration/test_session_orchestration.py`; persisted settings and
  storage failures are covered by `tests/server/routes/test_sessions_sharing_mode.py`.
- Live sandbox provisioning and native terminal-entered prompts require separate
  verification; local process checks do not cover them.
- Admin/UI component tests cover all three destination modes; server integration
  tests cover the matching current/other/managed/unbound admission matrix, and
  runner tests cover MCP refusal before creation.

## Gotchas

- "Created by" records origin only: workers remain independent and have their own
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
- A fresh managed sandbox is distinct from reusing an existing managed host.
  Even the most permissive setting offers only the clicked session's existing
  managed host; another family's managed host remains unavailable.
- Project moves skip the source check in no-auth and single-user local mode,
  where nobody else can hold an edit grant. A move only ever applies to the
  owner's own sessions; the PATCH remains owner-gated per session and family member.
- A receiver shared with edit access can continue working but cannot message
  back while shared that way. A read-only share does not stop it.
- Privacy checks govern these tools. They do not confine arbitrary code holding
  owner credentials or sanitize shared content the manager reads.
- The source token authenticates its runner, not an exact session within that
  runner. Runners are trusted for every session they host: another private owned
  session on the same runner can be named as the source. Stable-ID runner pools
  using allow-listed tunnel tokens are unsupported;
  this gate requires token-derived runner IDs and refuses other bindings.
- Side chats must have no edit-level grants and an owned, available source chain
  with none either to send work or create independent workers. Nested side chats
  and their children inherit this restriction. Missing or cyclic source chains are
  refused. Ordinary reads and incoming work remain available; removing the edit
  grants restores eligibility without erasing copied history.
- Read-only viewers of a manager see its transcript, including orchestration
  tool calls, worker session IDs and history it reads from the owner's other
  sessions. Owner-authorized reads already reach shared transcripts, so this
  adds no new disclosure path; workers still start private. The check uses stored
  grant levels, so a public grant capped below its stored level still refuses.
- Peer messages carry only task text and owner attribution. They do not forward
  per-event model/tool overrides or a caller-supplied delivery ID.
  The textual source prefix is a display hint, not an authorization credential
  or authenticated provenance. Other users can copy it into ordinary messages.
  V1 deliberately keeps this advisory text instead of adding structured provenance.
- Admission is the final source check before delivery or saving a startup task.
  A queued result means acceptance, not execution; destination policy may still
  deny the task during startup replay. Successfully queued tasks can finish after
  the sender is shared with edit access; replay retains
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
  The names also preserve different native approval scopes; see
  [why the messaging tools remain separate](../docs/session-messaging-tool-boundary.md).
- The server enforces the flag even though tool schemas are discoverable while
  it is disabled. Peer messaging has no additional per-agent opt-in; creation
  retains its existing `spawn` advertisement gate. Deploy the sibling-session
  schema before the application; peer messaging needs no schema migration.
  V1 treats eligible private agents as delegates across the owner's sessions.
  Unlike sharing, messaging does not grant another user access, but untrusted
  content can still influence an agent to steer other owned sessions. Enabling
  the flag accepts that broader trust; hiding a tool is not authorization.
