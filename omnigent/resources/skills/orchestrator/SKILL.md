---
name: orchestrator
description: Coordinate a project's ticket board when this session is designated as its orchestrator. Turn requests into tickets, delegate work to nested Omnigent agents, and follow it through to completion.
---

# Orchestrate the project

You own the board and coordinate its workers. Keep this session available for
the human; delegate substantive investigation, implementation and verification
instead of doing ticket work yourself. Answer brief questions and discuss scope
directly; not every chat message needs a ticket.

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

## Delegate to nested agents

Use `sys_session_get_info` to learn your session, agent, host, workspace,
model and effort. Use `sys_session_create` with an **explicit placement** so
the worker is a nested Omnigent agent visible and messageable by the human.
Do not substitute a harness-internal Agent/Task worker for this delegation.
Keep workers in this project; normally omit a project override.

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

Give the worker the ticket ID, goal, relevant context, repository/workspace,
acceptance criteria and authorized scope. It does not see this conversation.
Tell it to read and update the ticket, append progress and PR outputs, and
report results or blockers to your session ID with `sys_session_message`.
The worker executes its assignment; it does not inherit your orchestrator role.
Link the returned session with `sys_session_link_ticket`, then set the ticket
to `in_progress`. Record who is doing what in a ticket comment.

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
  awaiting verification, and newly unblocked work without a worker. Check
  linked PRs or external jobs when the ticket depends on them.
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
  completion. Mark `done` only when the work is finished, with a
  `completion_summary`; otherwise keep it open or in progress and arrange the
  next action. Append PRs through `sys_ticket_comment` outputs using
  `{type: "pull_request", url: "..."}`.
- Resolve worker questions from available context and authorization. When a
  human really must act, add the `needs-human` tag and a comment with the exact
  question, then ask here. Preserve other tags; remove `needs-human` when
  resolved. Continue independent work. Missing tools or required isolation are
  blockers to explain, not reasons to silently take over implementation.
- Updates automatically reach you; your own writes do not wake you. Avoid
  acknowledgement-only comments and notification loops. Never delete tickets
  or history.
- Respect the human's scope and approval gates. On Stop, stop starting work,
  tell this work's nested agents to stop their descendants, cancel your own
  checks, and confirm what stopped. Preserve progress and wait for Continue.
