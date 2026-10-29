# Session orchestration

An owner-authorized agent can send work to another session owned by the
same user, then read its status/history. It can also create private top-level
workers on its host or a new managed sandbox. This is enabled by default, with
`-session_orchestration` in `OMNIGENT_FEATURES` as a server kill switch.

Shared-source support remains under review. OpenAI Agents SDK, Codex-native
Chat and authenticated host-managed Codex terminal input have process evidence.
Claude SDK requester binding also has isolated pinned-process evidence.
Native Claude delegation is a documented limitation of this release; adding
edit sharing does not enable owner-only delegation on that path. Chat and
terminal typing remain available. Native Claude requester support is deferred,
not a release prerequisite. Provider-created helper delegation remains a
separate compatibility gap. Do not infer support from another harness's passing journey.

## Sub-features

- `message`: owner-authorized sender, same owner and workspace; shared destinations are
  allowed and their readers see the message. Normal queue/steering applies.
- `read-results`: the manager reads history/status; replies are not forwarded.
  Existing owner-authorized reads remain allowed when a session is shared.
- `created-by`: independent workers record their creating session. The worker's
  Chat/Terminal header rechecks access when opened, hides attribution during the
  lookup, and links to the creator when accessible; otherwise it says "Created by
  another session". `sys_session_list` and `sys_session_get_info` expose
  `created_by_session_id` so managers can identify their workers across restarts.
- `kill-switch`: an explicit disable refuses new peer sends and independent
  creates, even if an enable is also listed. Restart every server replica to
  apply it. Already accepted tasks may finish; ordinary reads and child work remain.
- `create`: `sys_session_create` retains child creation by default; `same_host`
  and `managed` placements create independent, initially private sessions.
  Managed creation uses the server's configured default sandbox provider.
- `sharing`: a verified owner execution may delegate from a shared sender.
  Collaborators may chat, but cannot authorize peer sends or independent workers.
  Consumed collaborator steering blocks further delegation in that execution;
  only a fresh owner execution restores it. Revoking or reducing a collaborator's
  access does not change queued input's requester. Side chats and children must
  preserve this restriction. Already accepted worker work continues.
- `scheduled`: scheduled runs, their forks and children use the new sends and
  independent creates like any private session their owner owns, so an
  automation can message the owner's other sessions or start workers.
- `refusal`: disabled flag, another owner, unverified requester or wrong runner binding
  refuses the operation, even from a private sender. Missing requester metadata
  never becomes owner authority. Unsupported servers reject independent creation without
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
- Use the existing Share dialog to share the sender or receiver, then repeat.
- Open an independent worker in Chat or Terminal and follow its "Created by"
  link. On mobile web, the same link is in the header. Ask the manager to list
  its sessions and match `created_by_session_id` to its own session ID.

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
  sharing behavior. Each harness checks shared owner requests, collaborator refusal
  for messaging and creation, fresh owner recovery, and owner side-chat delegation.
  The Codex row uses web submissions with provider consumption IDs and resolves
  each tool's retained execution through authenticated harness IPC. Its default
  row sends the first task and creates a worker through the real terminal WebSocket,
  including authenticated local single-user identity, then exercises Chat on the
  same executor. Reattachment uses the same observed terminal. Provider-native
  helper delegation and the legacy internal standalone launcher are not covered.
  The public Codex CLI uses host-managed preparation. A separate isolated
  process drive at `d3b563fd6` verified typed owner delegation through the public
  command with a preconnected host; first-time host onboarding remains unverified.
  Claude SDK rows require the lockfile SDK and pinned CLI inside a clean Linux
  container with loopback-only networking; they skip before probing Claude on
  an ordinary workstation. The isolated CLI 2.1.295 / SDK 0.2.121 rows passed
  at `d3b563fd6` with a scripted provider, no external network and no host mounts.
  A workstation skip is not Claude process validation; the SDK proof does not
  cover native Claude terminals or provider-created helpers.
  After a runner restart, repeat typed input in the existing Codex session.
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_resume_clamps_persisted_effort`
  and `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_terminal_only_work_survives_runner_inactivity`
  cover resumed typing, preserved thread/model settings, and active terminal work
  surviving the runner's idle timer. Initial-launch success does not cover recovery.
  It also checks that unconfigured managed creation gives a clear
  refusal without creating a session. Codex and tmux are required for the native row.
  All three harnesses also exercise a server restart with the explicit kill switch:
  existing runners remain opted in, but their subsequent peer sends and independent
  creates are refused without writes; session-info reads still work.
  It also fires an owner-created schedule through the real server/host and checks
  that the resulting worker can message a peer and create an independent worker.
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
- Browser manual check for OpenAI Agents SDK and Codex-native Chat: own A and B, share both with Pat
  as an editor, then send a distinctive task through A as the owner. Confirm B
  receives it. Repeat through Pat's Chat: expect refusal and no new task in B.
  Submit a fresh owner message and confirm delegation works again. Queue/steer
  Pat's message during owner work, reduce Pat to read-only, and verify that old
  work does not gain owner authority. Repeat independent worker creation and
  check that a refused request leaves no new worker.
- Public Codex CLI manual check: in an isolated environment with a connected
  owned host and a scripted provider, run `omnigent codex --server <url>`.
  Type a request to send a uniquely named task to an owned peer using
  `sys_session_message`. Confirm the actual tool result is queued, the peer's
  persisted input names the source session, and its reply arrives. Type `/quit`
  and check exit status. Use the pinned CLI and retain the command and terminal
  transcript. Do not substitute an HTTP prompt, direct terminal WebSocket write,
  or initial `-p` argument for the interactive public entry point. This case
  does not prove first-time onboarding, independent creation or multi-user CLI auth.
- Requester compatibility matrix (implementation limits, not test results):

  | Entry point | Owner delegation | Required verification |
  | --- | --- | --- |
  | OpenAI Agents SDK Chat/API | Implemented | Real process send/create, editor refusal, fresh owner recovery and kill switch. |
  | Codex native web Chat | Implemented | Pinned CLI consumption IDs, authenticated harness lookup, browser actions and destination history. |
  | Claude SDK Chat/API | Implemented | Raw provider UUID echoes and exact SDK control tasks retain input/tool lineage before typed parsing. Isolated pinned-process rows cover owner/editor/recovery, worker creation and kill switch; component tests cover delayed callbacks. |
  | Claude native Chat/terminal | Unavailable; deferred | Edit sharing and typing remain available. Peer sends and independent worker creation refuse without verified requester binding, including from private sources. Native Claude requester support is not a release prerequisite. |
  | Codex host-managed terminal / default CLI attach | Implemented | Authenticated relay captures writers; the TUI proxy registers input before submission. Real terminal WebSocket send/create passes. A separate public CLI drive proves typed owner send with a preconnected host and local single-user auth; first-time onboarding and multi-user CLI auth remain unverified. |
  | Legacy internal Codex standalone launcher | Delegation blocked | No in-tree production caller found; the public CLI uses host-managed preparation. Direct internal calls lack trusted input registration. |
  | Provider-native children/helpers | Incomplete | Preserve each input's requester through child creation, steering, queue consumption and wakes. Ordinary helper visibility is not proof. |
  | Legacy runner → updated server | Refused without requester | Private/shared sends and creates leave no new task or worker; ordinary child work keeps unknown origin. |
  | Updated runner → older/mixed server replicas | Refuses unsupported receiving routes | Mutations require `/v1/requester/sessions` and its events route, with no legacy fallback. ASGI route-absence tests include servers that already have the older orchestrated endpoint; no released-binary mixed-replica process run. |

- Native Claude mod contract probe: the opt-in developer plugin in
  [the probe guide](../dev/claude_requester_probe/README.md) observes typed input,
  turn/tool identifiers and MCP metadata. Run
  `tests/e2e/test_claude_mod_requester_probe.py` only in its isolated Linux
  environment; `tests/dev/test_claude_requester_probe.py` covers handler/analyzer
  contracts without Claude. This is diagnostic evidence, not owner delegation
  support. No production entry point installs the plugin, and the blocked row
  above remains unchanged. The probe also observes transcript row IDs and tests
  `/clear` followed by fresh work. Its [remaining contract](../dev/claude_requester_probe/REQUESTER_CONTRACT.md)
  separates supported row/tool identity from the missing input-consumption proof.

- Component compatibility checks: `tests/runtime/harnesses/test_executor_adapter_requester.py`
  exercises retained callbacks with the real SDK parser/dispatcher. The Claude
  scenarios verify retained SDK callback binding and that lost metadata stays
  unknown even after fresh owner input. These are component checks, not Claude
  process evidence.
  `tests/server/integration/test_session_orchestration.py` checks owner/editor/
  missing requester across child startup, peer delivery, sharing changes and
  durable queues. `tests/inner/test_acp_executor.py` and
  `tests/test_sessions_native_messages.py` cover other affected consumers of
  shared dispatch metadata. Run these as ordinary pytest targets, with retries
  disabled; they do not replace process or terminal evidence.
- Native compatibility check for each supported requester path: send a distinctive task to a second session;
  open its Chat and confirm the source label and result. Ask the manager to
  read it. Share the manager and repeat as owner, then collaborator: only the
  owner request may reach the destination. An unsupported shared-source path
  must refuse. API runs do not prove these UI actions.
  Append `-session_orchestration` to the isolated server's feature settings and
  restart it, then repeat from a private manager: expect a disabled error, no new
  task and no independent worker. Read history/info to confirm reads still work.
- Live sandbox provisioning, native Claude and legacy standalone delegation require
  separate verification; local host-managed Codex process checks do not cover them.

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
- Runner creation and message mutations use the internal `/v1/requester/sessions`
  route family, which reuses the ordinary handlers but cannot exist on a server
  predating requester preservation. There is no fallback to ordinary or legacy
  orchestrated routes. Public UI/API routes keep their existing paths.
- Codex terminal identity accumulates across the terminal process incarnation.
  Unknown or mixed writers keep terminal-origin delegation restricted until the
  terminal restarts; reconnecting cannot clear it. Fresh authenticated owner Chat
  starts an independent execution. This conservative terminal rule does not claim
  per-prompt recovery for an ambiguously shared composer. Generic runner-bearer
  attachments and programmatic terminal writes establish no human identity.
- A shared receiver can continue working. A verified owner execution can send
  another message; sharing does not add automatic completion replies.
- Requester and ownership checks govern these tools. They do not confine arbitrary code holding
  owner credentials or sanitize shared content the manager reads.
- The source token authenticates its runner, not an exact session within that
  runner. Runners are trusted for every session they host: another private owned
  session on the same runner can be named as the source. Stable-ID runner pools
  using allow-listed tunnel tokens are unsupported;
  this gate requires token-derived runner IDs and refuses other bindings.
- Side chats require an owned, available source chain. Verified owner executions
  may delegate through shared chains; missing requester identity refuses even
  when every source is private. Nested side chats and children inherit the checks. Missing or cyclic
  chains are refused. Sharing changes never rehabilitate a known collaborator's
  queued input or consumed steering.
- Peer messages carry task text, display attribution, and protected requester
  metadata on authenticated transport. They do not forward
  per-event model/tool overrides or a caller-supplied delivery ID.
  The textual source prefix is a display hint, not an authorization credential
  or authenticated provenance. Other users can copy it into ordinary messages.
  Requester authority comes from the protected envelope, never this text.
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
  V1 treated eligible private agents as delegates across the owner's sessions;
  this change additionally requires a verified owner execution.
  Unlike sharing, messaging does not grant another user access, but untrusted
  content can still influence an agent to steer other owned sessions. Enabling
  the flag accepts that broader trust; hiding a tool is not authorization.
