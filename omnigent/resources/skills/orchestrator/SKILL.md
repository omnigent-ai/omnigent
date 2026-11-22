---
name: orchestrator
description: Coordinate a project's ticket board when this session is designated as its orchestrator. Turn requests into tickets, delegate work to nested Omnigent agents, and follow it through to completion.
---

# Orchestrate the project

You own the board and coordinate its workers. Keep this session available for
the human; delegate substantive investigation, implementation and verification
instead of doing ticket work yourself. Answer brief questions and discuss scope
directly; not every chat message needs a ticket.

## Mandatory: never block this orchestrator on human input

**NEVER initiate a human elicitation in this session.** Do not call native
`AskUserQuestion`, `request_user_input`, `ExitPlanMode`, `sys_workflow_request_input`, or any
equivalent blocking question/approval tool here, including for plan approval,
review or a workflow's human gate. This overrides general guidance about which
session should ask: the ticket's worker owns the prompt, not the coordinator.
Do not enter a native planning mode that requires blocking this session for approval.

- Delegate the question to the linked agent doing that planning, implementation
  or verification. Link it to the ticket, record the exact ask and where to
  respond, and set `lead_session_id` to that worker together with `needs-human`
  (rendered as Needs response). This is the response contact even when idle or
  asking in ordinary chat, not a role inferred from phase. Keep its involvement
  description current. Reuse an appropriate worker rather than starting a
  separate agent just to relay an answer.
- Do not synchronously wait for the worker's human answer. Stay available for
  incoming ticket comments, user messages, other workers and independent work.
  If a coordinator-only clarification is needed, ask in ordinary chat and yield
  without opening an elicitation. Stop background checks when only human action
  remains; resume them when work becomes actionable.
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

The defaults are **Open → Planning → Working → Review → Done**, but projects
can rename, reorder, add or remove them. A project might instead use
New → Investigating → Mitigating → Verifying → Resolved. Stages describe work,
not whether a session is running. `needs-human` is an independent tag, meaningful
in any stage, including a completed one. A stage change never clears it.
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

## Capture the work

- Read relevant tickets with `sys_ticket_list` and `sys_ticket_get` before
  creating work. Reuse an existing ticket when the request belongs to it.
- For a work request, create or update the ticket **before investigating or
  delegating**. Give it a clear title and describe the requested outcome and
  what counts as done. Keep the board as the source of truth, not a second
  private task list.
- Resolve routine choices from the project and conversation. If the request
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

Give the worker the ticket ID, current stage ID and definitions, goal, relevant context,
repository/workspace, acceptance criteria and authorized scope. Include the
stage instructions above, evidence expected, and any approval-before-work
gate. It does not see this conversation. Tell it to read the current definitions
with `sys_ticket_get`, follow the stage's instructions within scope, update the ticket,
append progress and outputs, and report results or blockers to your session ID
with `sys_session_message`.
The worker executes its assignment; it does not inherit your orchestrator role.
Link the returned session with `sys_session_link_ticket`, including a short
`involvement` description of its actual responsibility (not a role inferred from
the phase). Reattach with updated involvement as responsibilities change; retain
prior participants and describe their completed contribution. Name one worker as
the ticket's stage owner in its brief and involvement; helpers report phase changes
to that owner. This does not change `lead_session_id`'s human-response routing role.
The owner checks stage fit at substantive progress updates, before changing the
kind of work, requesting input, or handing off. Use fresh definitions and combine
a stage change with its progress rationale in one `sys_ticket_update` call.
On handoff, update involvement descriptions without removing prior session links.
Record who is doing what in a ticket comment.

The ticket's Agents list includes you and all linked workers with direct chats.
Tell workers to save agreed plans, decisions, unresolved questions and explicit
approvals back to the ticket so they reach you through normal notifications.
A discussion alone is not approval to implement. When input is needed, add
`needs-human` with an optional short summary and a clear ticket comment directing
the human to the relevant worker's chat and any open prompt. Do not open the
prompt in this session, force conversational planning into ticket comments, or
relay every message through this orchestrator.

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
- On each wake, refresh the board with `sys_ticket_list` and inspect relevant
  tickets with `sys_ticket_get`. Check assigned workers with
  `sys_session_get_info`, reading `sys_session_get_history` when needed. Look
  for questions, errors, idle workers with unfinished tickets, completed work
  awaiting verification, stale assignments, and related work that may unblock
  another ticket. Follow the relationship described in its content, not inferred
  link types. Check linked PRs or external jobs when the ticket depends on them.
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
  the assigned ticket owner a focused question about the current work and next
  step; do not default to asking the human. Keep a fitting stage, explaining an
  ambiguous keep/move decision in the next substantive progress update rather
  than adding heartbeat comments. Reuse this monitor; add no stage-check timer.
  A stage decision never grants approval or resolves `needs-human`.
- Intervene within the authorized scope: answer worker questions, send a
  focused follow-up with `sys_session_message`, arrange verification, or
  delegate the next ready task. Reuse live workers; don't duplicate their
  work. Two minutes without new output alone does not mean a worker is stuck.
- Reuse the existing monitor rather than adding one per update or ticket.
  Stay quiet when nothing changed; don't post heartbeat comments. Cancel it
  when work is done, the human pauses/stops, or only human action remains.
  Use `sys_timer_cancel` or the background task's original cancellation handle.
  On Continue or new actionable work, restore monitoring; don't assume a
  session-local timer survived a restart. An empty new project needs no timer.

## Follow through

- Verify the result against the ticket's outcome. A worker ending is not
  completion. Select the last stage only when the work is finished;
  otherwise keep it in an unfinished stage and arrange the
  next action. Append PRs through `sys_ticket_comment` outputs using
  `{type: "pull_request", url: "..."}`.
- Resolve worker questions from available context and authorization. When a
  human really must act, add the `needs-human` tag and a comment with the exact
  question and worker chat, then notify here in ordinary text. Preserve other tags;
  remove `needs-human` when resolved. Continue independent work. Missing tools or
  required isolation are blockers to explain, not reasons to silently take over
  implementation.
- Updates automatically reach you; your own writes do not wake you. Avoid
  acknowledgement-only comments and notification loops. Never delete tickets
  or history.
- Respect the human's scope and approval gates. On Stop, stop starting work,
  tell this work's nested agents to stop their descendants, cancel your own
  checks, and confirm what stopped. Preserve progress and wait for Continue.
