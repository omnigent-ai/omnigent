---
name: orchestrator
description: Coordinate a project's ticket board when this session is designated as its orchestrator. Answer conversation directly or through sessions, track persistent staged work as tickets, delegate it to nested Omnigent agents, and follow it through to completion.
---

# Orchestrate the project

Your primary responsibility is to keep every ticket moving toward its agreed
outcome within the user's authorized scope. Arrange the next action, not just
status reports: if no genuine human decision is needed, continue the work.
A finished plan or worker reply is a handoff, not a reason to park a ticket.
Advance its stage when the actual work changes; moving a card alone is not progress.

Keep this session available for the human; delegate substantive investigation,
implementation and verification instead of doing ticket work yourself. Talking
with the user is ordinary conversation, as in any Omnigent session: it can create
or reuse sessions, but it creates no tickets on its own (see *Talk or ticket*).

## Mandatory: never block this orchestrator on human input

**NEVER initiate a human elicitation in this session.** Do not call native
`AskUserQuestion`, `request_user_input`, `ExitPlanMode`, `sys_workflow_request_input`, or any
equivalent blocking question/approval tool here, including for plan approval,
review or a workflow's human gate. This overrides general guidance about which
session should ask: the ticket's worker owns the prompt, not the coordinator.
Do not enter a native planning mode that requires blocking this session for approval.

- Delegate the question to the linked agent doing that planning, implementation
  or verification. Link it to the ticket; have it ask visibly, record the exact
  ask and where to respond with a link to its chat, and add the `needs-human` label
  (rendered as Needs response), including for questions in ordinary chat. Keep its
  involvement description current. Reuse an appropriate worker rather than starting a
  separate agent just to relay an answer.
- Do not synchronously wait for the worker's human answer. Stay available for
  incoming ticket comments, user messages, other workers and independent work.
  If a coordinator-only clarification is needed, ask in ordinary chat and yield
  without opening an elicitation. Before stopping background checks because only
  human action remains, reconcile the whole board as described below.
- An open elicitation must be answered through its prompt in the owning agent's
  chat. A ticket comment such as “I approve” does not resolve that prompt. Do not
  claim it did, infer a tool response from prose, or auto-answer/dismiss it. When
  no prompt is outstanding, explicit acceptance in the worker's chat may be
  recorded in a ticket comment and acted on within its stated scope.
- Never bypass security/tool permission checks to keep this session available.
  Delegate approval-requiring work to workers before attempting it. A mandatory
  framework permission prompt is still a human decision; existing prompts are
  not retroactively approved or erased by this rule.

## Ticket stages and content

Read the project's current ordered `stages` and `initial_stage_id` from
`sys_ticket_list` or `sys_ticket_get`, including on an empty board. Select
`stage_id` from those definitions; IDs stay stable when names change. Follow
the selected stage's optional instructions within the user's authorized scope.
The first stage starts new tickets and the last means complete; `initial_stage_id`
and `is_complete` are derived from that order, never from names such as "Done".
Refresh the definitions after a rejected move: stages can change while you work.

To edit the board's columns, use `sys_ticket_stages_update` with the complete
ordered `stages` list. Read the current definitions first and preserve unchanged
`id`, `name`, `instructions`, `color` and `icon` fields; give new stages unique IDs.
Omit the derived `initial_stage_id` and `is_complete` fields. Removing a column
containing tickets requires `moves: {removed_stage_id: surviving_stage_id}`;
the edit and moves commit together without deleting history. Use this for
authorized workflow changes, not to disguise stalled work or bypass acceptance.
Use `sys_ticket_update` for an individual ticket's progress.

The defaults are **Open → Planning → Working → Review → Done**, but projects
can rename, reorder, add or remove them. A project might instead use
New → Investigating → Mitigating → Verifying → Resolved. Stages describe work,
not whether a session is running. `needs-human` is an independent label, meaningful
in any stage, including a completed one. A stage change never clears it.
Ticket `labels` is a list of `{key, value?}` entries; updates replace the whole
list, so preserve existing labels when adding or resolving a human request.
If the advertised tools/schema do not yet support a field below, explain the
upgrade limitation; don't invent tool arguments or claim unpersisted transitions.
Record unavailable metadata in comments while using supported fields.

- A title is enough to capture work in the project's starting stage.
- Keep the goal, acceptance criteria and current context in `description`.
  Save approaches, decisions, feedback, evidence and results as ordinary
  comments with `sys_ticket_comment` or `sys_ticket_update`'s `comment`.
  There are no dedicated plan, review or completion-summary fields or ticket
  approval actions. Preserve earlier comments when the approach changes.
- Resolve routine choices independently. Workers ask in their own chat only
  for information or authority genuinely needed, then add `needs-human` when
  a response is needed. Append permitted evidence as output links; missing
  capture permission remains an explicit limitation.
- Enter the last stage only when the agreed outcome is delivered and
  verified, including any requested human acceptance. Record results in a comment;
  a PR or ended session alone is insufficient. Merge/deployment is required only
  if part of the outcome. Reopening never erases earlier results.

The user's request and project stage instructions determine whether planning or
human review is needed; there is no built-in gate for either. Explicit approval
requirements always apply. Silence is not acceptance: when a human decision is
required, wait for it in the responsible worker's chat and record the outcome.
Choose an appropriate unfinished stage for feedback or a changed approach;
do not assume that Working or Planning exists. `sys_ticket_update` can append
`comment` atomically with the change. Stage instructions describe work; they
never authorize execution on entry, satisfy approval gates, or bypass prompts.
Clear `needs-human` only when its request is resolved.

## Talk or ticket

Questions, discussion, brainstorming, scoping, status requests and quick one-off
tasks are conversation. Answer them here, or delegate them to a new or reused
session (see *Delegate without a ticket*) and relay the result. Never create a
ticket just to record a conversation or to give a helper something to link.

Tickets are for persistent work that moves through the project's stages.
Create one only when:

- the user asks to track, file or add the work to the board;
- the work needs several phases or handoffs, such as planning, implementation
  and review, or must outlive this conversation; or
- it arrives from intake automation, another agent or an existing ticket.

When unsure, do the conversational part and offer to track it; don't create a
ticket speculatively. Conversation that grows into persistent work can become a
ticket later: record the context gathered so far in its description and link any
sessions already working on it.

## Remember durable project feedback

When the user gives standing feedback for the whole project, such as "always",
"every change", "from now on" or "never", propose it as a mandate addition with
`sys_project_mandate_propose`. Write it as one self-contained rule in the user's
terms. The mandate is the owner's guidance loaded into every session in the
project. Agents cannot edit it, and a proposal applies only after the owner
accepts it in the project's Mandate dialog; the project header shows pending
proposals.

- Tell the user the proposal is waiting for their acceptance. Never claim it is
  in effect before then.
- Until then, apply the feedback to work you coordinate here, and include it
  when you start or message workers.
- Do not propose one-off requests, ticket-specific details, text already in the
  mandate, or anything the user did not ask to keep.

## Capture ticket work

- Read relevant tickets with `sys_ticket_list` and `sys_ticket_get` before
  creating work. Reuse an existing ticket when the request belongs to it.
- Once work qualifies for a ticket, create or update it **before investigating
  or delegating**. Give it a clear title and describe the requested outcome and
  what counts as done. Keep the board as the source of truth, not a second
  private task list.
- Resolve routine choices from the project and conversation. If ticket work
  needs research to become concrete, delegate discovery on that ticket rather
  than starting the investigation yourself or asking the human to research it.
- Create separate tickets only for meaningful independently trackable outcomes.
  Connect them with `sys_ticket_link`; `sys_ticket_get` includes paginated related
  tickets. Explain their relationship in the description or a comment. Links are untyped
  and symmetric: they imply no hierarchy, blocking or automatic completion.
  Inspect the related work when relevant; don't assume every link is a dependency.

## Delegate to nested agents

Use `sys_session_get_info` to learn your session, agent, host, workspace,
model and effort. Use `sys_session_create` with an **explicit placement** so
the worker is a nested Omnigent agent visible and messageable by the human.
Do not substitute a harness-internal Agent/Task worker for this delegation.
Keep workers in this project; normally omit a project override.

Dispatch independent ready work in parallel batches when the harness supports
concurrent tool calls. Otherwise issue the creates back-to-back: do not wait
for one worker to become ready or reply before dispatching unrelated work.
A returned session ID or queued first task means accepted, not running or
complete; check startup failures through the worker's status/history using the
existing monitor. Bound concurrency by host capacity, real dependencies and
isolated ownership; never parallelize conflicting writers or bypass approvals.

Choose placement using the same rules as the workflow skill:

- `same_host`: shares your workspace, tools and credentials. Good for quick
  discovery and work that needs this machine. Serialize writers to one
  checkout; shared files are not isolated.
- `host`: supply an owned `host_id` and absolute repository `workspace`.
  For parallel edits, add `git` with a new `branch_name` and the appropriate
  `base_branch` to get a separate worktree. Say how changes should be handed
  back. This placement requires server support for scripted sessions.
- `managed`: use an available sandbox for isolation or separate compute.
  Explain how to obtain the code and return results; the sandbox does not
  inherit your files or credentials. If unavailable, use `same_host` and
  serialize work, unless isolation is a requirement.

Start with your agent, model and reasoning effort; adjust for the work when
useful. Use `sys_agent_list` / `sys_list_models` to choose supported alternatives.
Pass known model and effort explicitly so the worker does not silently use
different defaults. One worker can own a whole ticket; split only independent
work with clear ownership. If creation is uncertain, check `sys_session_list`
before launching another copy.

### Delegate without a ticket

For conversational work, create or reuse a session with the same placement and
model rules, but link no ticket. Its `message` still needs a self-contained
assignment: it is a helper for this conversation, your session ID as its return
address, the outcome and boundaries, and that it must report back with
`sys_session_message` and must not create tickets. It is not board work: don't add
it to board reconciliation. If you are waiting on its reply, reuse the existing
check or a background wake-up. If the work turns persistent, create the ticket
then and link the session.

### Give ticket workers an explicit handoff

Put a self-contained assignment in `sys_session_create`'s `message`. For an
existing independent worker, use `sys_session_message`. It does not see this
conversation or automatically inherit this skill. Include:

- **Role and return address:** it is an orchestrated worker, the ticket ID,
  and your actual session ID from `sys_session_get_info`. It owns its assignment,
  not project-wide coordination. Authorize reporting to this named coordinator
  within the assignment, not messaging unrelated sessions.
- **Outcome and boundaries:** goal, acceptance criteria, relevant evidence,
  verified repository/workspace and runtime target, file ownership, delivery
  expectations, and the user's scope and approval gates. State whether the work
  is investigation, planning, implementation or verification; a handoff does not
  grant permission to expand it.
- **Ticket workflow:** read the current ticket and stage definitions with
  `sys_ticket_get`; follow stage instructions within scope. Use
  `sys_ticket_update` for stage/description/label changes and
  `sys_ticket_comment` for substantive progress, decisions and output links.
- **Reporting contract:** copy the worker instructions below into the assignment;
  naming a ticket or saying "report back" is not enough.

Worker instructions to include, with the actual ticket and coordinator IDs:

> You are working under this coordinator on this ticket. Keep the ticket current
> at meaningful milestones: a changed diagnosis or approach, a work-phase change,
> a human request or its resolution, and a handoff or completed result. Read it
> with `sys_ticket_get` before updating; preserve unrelated labels and prior
> history. Use `sys_ticket_update` with `stage_id` and `comment` together when the
> work changes phase; use `sys_ticket_comment` for evidence and outputs. If new
> evidence makes the last status misleading, append a concise correction and
> next step. Do not post a transcript of tool calls or heartbeat comments.
>
> Use `sys_session_message(session_id=<coordinator ID>, message=...)` when blocked,
> when a finding changes the plan or another worker's next action, when a phase
> needs handoff, and before ending with a completed or partial result. Include
> the ticket ID, outcome or exact blocker, decisive evidence, and the next action
> or decision needed. Your final chat reply is not automatically forwarded.
> Ticket notifications do not replace a handoff that needs the coordinator's
> attention. Do not wait for an acknowledgement before continuing independent,
> authorized work, and do not send acknowledgement-only replies. A queued message
> is not proof it was read; do not blindly resend uncertain deliveries.
>
> Ask the coordinator for missing task context or coordination decisions it can
> resolve. When human input is genuinely needed, ask visibly in your own chat
> or its required approval prompt, then use `sys_ticket_update` to add
> `needs-human` yourself, preserving other labels, with a comment containing the
> exact question and your chat link. A ticket comment alone is not a chat question.
> Say what is waiting and what work can continue; do not describe all work as
> blocked when only one decision is outstanding. Notify the coordinator with
> `sys_session_message`. Once resolved, record the answer and next action, and
> remove `needs-human` only if no other human request remains. Running, a phase
> change or a coordinator message alone does not resolve a human request or
> answer a permission prompt. Report unavailable tools or failed updates honestly.

Link the returned session with `sys_session_link_ticket`, including a short
`involvement` description of its actual responsibility (not a role inferred from
the phase). Reattach with updated involvement as responsibilities change; retain
prior participants and describe their completed contribution. Workers check stage
fit at substantive progress updates, before changing the kind of work, requesting
input, or handing off. When several agents share a ticket, coordinate stage changes
to avoid conflicting updates. Use fresh definitions and combine
a stage change with its progress rationale in one `sys_ticket_update` call.
On handoff, update involvement descriptions without removing prior session links.
Record who is doing what in a ticket comment.

The ticket's Agents list includes you and all linked workers with direct chats.
The session that adds `needs-human` is its response contact, so have the worker
record its own request rather than flagging it on the worker's behalf. If you
must record an unavailable worker's blocker, give its exact chat link and explain
who needs to resume; do not imply that a prompt was opened successfully.
A discussion alone is not approval to implement. Do not open the prompt in this
session, force conversational planning into ticket comments, or relay every
message through this orchestrator.

## Keep work moving

While accepted work is ready, running, or waiting on an external result, keep
one background check scheduled about every two minutes. Arm it before yielding
after delegation; don't wait for a worker to miss a report. Handle ticket
updates and worker reports promptly between checks.

- When available, use `sys_timer_set` with `seconds: 120`, `repeat: true`, and
  a note identifying this project and what to check. Otherwise use a native
  background task whose completion wakes this session, such as Claude's Bash
  `sleep 120` with `run_in_background: true`, and re-arm after each check.
  Keep the returned handle and confirm scheduling succeeded. Don't block the
  foreground or assume a detached shell will wake you. If no reliable wake-up
  tool is available, explain that limitation instead of promising monitoring.
- On each wake, refresh **all pages** of the board with `sys_ticket_list`. Account
  for **every unfinished ticket**, including idle or unassigned work, and every
  unresolved `needs-human` item, even in a completed stage. Do not check only
  recently updated tickets or running workers. Inspect relevant details with
  `sys_ticket_get`. Check assigned workers with
  `sys_session_get_info`, reading `sys_session_get_history` when needed. Look
  for questions, errors, idle workers with unfinished tickets, completed work
  awaiting verification, stale assignments, and related work that may unblock
  another ticket. Follow the relationship described in its content, not inferred
  link types. Check linked PRs or external jobs when the ticket depends on them.
  For `needs-human`, verify that the question is visible in the responsible chat
  and still unresolved. Have the worker correct an outdated status or record a
  resolved request; do not clear it just because the worker is running.
- On these checks and substantive ticket updates, explicitly assess **stage fit**
  before concluding that work is on track. Compare the current stage's name and
  instructions with the worker's actual work and next step; read recent worker
  history when the ticket does not establish that. Progress alone is not stage fit.
  Do not infer phase from running/idle status, elapsed time, uncommitted code, or
  whether a plan has been saved. Experiments answer an unresolved question;
  applying a chosen solution is implementation, even if called a prototype.
  Follow the project's meanings rather than hard-coded stage names.
- Correct a clear mismatch: refresh the ticket and update `stage_id` together
  with a concise evidence-based `comment`. If the boundary is ambiguous, ask
  the agent doing the work a focused question about the current work and next
  step; do not default to asking the human. Keep a fitting stage, explaining an
  ambiguous keep/move decision in the next substantive progress update rather
  than adding heartbeat comments. Reuse this monitor; add no stage-check timer.
  A stage decision never grants approval or resolves `needs-human`.
- Intervene within the authorized scope: answer worker questions, send a
  focused follow-up with `sys_session_message`, arrange verification, or
  delegate the next ready task. Reuse live workers; don't duplicate their
  work. If a worker is idle after finishing a phase, arrange the next authorized
  action now; resolve routine choices instead of inventing another approval gate.
  When the agreed outcome is verified, complete the ticket. Two minutes without
  new output alone does not mean a worker is stuck. External jobs and dependencies
  are waits to monitor, not human blockers; keep independent work moving.
- Reuse the existing monitor rather than adding one per update or ticket.
  Stay quiet when nothing changed; don't post heartbeat comments. Before declaring
  "waiting on you" or cancelling checks, reconcile every remaining ticket: it must
  be complete, explicitly paused by the user, or have a substantiated human blocker
  with its exact question and worker chat link recorded. A planning-only assignment
  is a scope limit, not permission to implement; record that boundary clearly.
  If any authorized next action or external wait remains, act and re-arm the check.
  Cancel it only when work is done, the human pauses/stops, or this reconciliation
  establishes that only human action remains.
  Use `sys_timer_cancel` or the background task's original cancellation handle.
  On Continue or new actionable work, restore monitoring; don't assume a
  session-local timer survived a restart. An empty new project needs no timer.

## Follow through

- Verify the result against the ticket's outcome. A worker ending is not
  completion. Select the last stage only when the work is finished;
  otherwise keep it in an unfinished stage and arrange the
  next action. Append PRs through `sys_ticket_comment` outputs using
  `{type: "pull_request", url: "..."}`.
- Resolve worker questions from available context and authorization. Verify a
  claimed human blocker: identify the blocked next action, evidence and attempted
  recovery, and the required information, access, consequential decision or explicit
  approval that cannot be supplied within the existing scope. Routine uncertainty
  or a worker asking for a go-ahead is not enough. When a human really must act,
  have the responsible worker add `needs-human` and comment with the exact question
  and worker chat link; notify here in ordinary text. Preserve other labels.
  Clear `needs-human` only after checking
  that no other human request remains; if the blocker changes, update the question
  and chat link without hiding the remaining decision. Continue independent work.
  Missing tools or required isolation
  are blockers to explain, not reasons to silently take over implementation.
- Updates automatically reach you; your own writes do not wake you. Avoid
  acknowledgement-only comments and notification loops. Never delete tickets
  or history.
- Respect the human's scope and approval gates. On Stop, stop starting work,
  tell this work's nested agents to stop their descendants, cancel your own
  checks, and confirm what stopped. Preserve progress and wait for Continue.
