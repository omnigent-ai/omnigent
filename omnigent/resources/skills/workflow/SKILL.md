---
name: workflow
description: Run a multi-step workflow from a description or one of the user's saved workflows. Keeps the plan and every step's status in the session's journal, so people can follow along in the Tasks tab and the work resumes cleanly.
---

# Run a workflow

The user described the work after `/workflow`, or named one of their saved
workflows. Turn it into a plan, then work through the plan, recording where
each step stands as you go. The user watches
the plan's steps as tasks in the session's Tasks tab and can steer or stop you
in chat at any time.

For a request to design, edit or save a reusable workflow, including a workflow
page's planning chat, use the [save-workflow skill](../save-workflow/SKILL.md).
Research the procedure and publish a proposal without executing it. The
execution instructions below apply when the user asks to run the work.

You need the `sys_journal_*` tools, plus `sys_workflow_load` and
`sys_workflow_list` for a saved workflow. If a required tool is missing, report
the specific limitation. `sys_workflow_request_input` is only needed when a
person must answer; its absence does not block work you can do independently.

## Work independently

- Carry the requested work through to completion within its scope and existing
  authorization. Use the conversation, journal, saved workflow and project
  context before asking the user to supply information or make a choice.
- Resolve routine ambiguity with a reasonable, reversible default. Record
  important assumptions in a note and proceed. Do not pause for naming,
  implementation choices, optional preferences or confirmation to continue.
- A request to run a workflow, including an automation's configured request,
  authorizes the actions it clearly calls for within its scope and existing
  permissions. Do not require a fresh go-ahead for each step. An action being
  external or important does not by itself require another approval. Honor
  explicit review gates and restrictions imposed by policy or tools.
- CI, background jobs and delegated work are things to monitor, not questions
  for the user. Keep working on independent steps, and use the recovery and
  delegation guidance below before treating a problem as a human blocker.

## Before asking or declaring a blocker

First check the relevant conversation, journal, project files and available
tools. Use documented defaults or choose a reversible implementation that
meets the request. If an operation fails, inspect the error and try applicable
recovery or an equivalent authorized way to finish it. Do all preparation and
independent work that can proceed. Do not retry an explicit permission denial.

Human intervention is justified only for one of these concrete obstacles:

- A required fact, artifact or access is unavailable after relevant lookups,
  and the named operation cannot produce a correct result without it. State
  exactly what is missing; an optional preference or extra reassurance does
  not qualify. Do not invent data, credentials or successful results.
- An unresolved choice would cause different irreversible external effects.
  Identify the alternatives and their consequences. Continue reversible
  preparation; ordinary naming, implementation and scheduling choices are
  yours to make.
- An explicit instruction, applicable policy or enforced tool gate requires
  a human review, approval or action that has not been satisfied. Cite that
  requirement and check the existing request and authorization first. Do not
  infer a gate from a general preference for caution.

Before asking or writing `failed` for one of these obstacles, record the
blocked operation, the evidence, the lookups or recovery attempted and the
exact input or action needed. Explain why a default or authorized alternative
cannot finish that work. "Needs clarification", "might need approval" and
"to be safe" are not sufficient reasons. This requirement applies to workers
too; check their evidence before accepting a claimed blocker. Block only the
affected work and keep independent steps moving.

## When another workflow started you

Check this first. If your first message says another session started this
workflow, that session is waiting on you. When your plan finishes, with every
step done or failed, message that session with `sys_session_message`, saying
how it went and what's left. This is very important: don't skip it, even if
you also tell the user, since the other workflow can't go on without your
message. Report blockers to the starting session too, with the evidence and
what you need. It may already have the answer or authorization; don't send
the user to a child session to repeat information.

## When an automation runs you

Check this too. If your message says no one is watching this run, as an
Omnigent automation's note does, no one can answer you or approve anything,
so nothing in the run may wait for a person:

- Run already-authorized work without a plan-review pause. An unattended run
  does not grant missing approvals or override decisions reserved for the user.
- Choose reversible defaults within that authorization, and record what you
  chose and why in the step's status. Don't use `sys_workflow_request_input`,
  and write no `needs_input`.
- Apply "Before asking or declaring a blocker" before writing `failed` for
  missing input or approval. Being unattended, uncertainty or a worker's
  question is not itself a failure. Resolve decisions within the authorized
  scope; fail only the affected step when the concrete obstacle remains,
  with the required evidence. Do not perform an action whose required
  approval is missing. Go on with steps that do not depend on it.
- Every subagent, task and session you start inherits these constraints.
  Include the unattended context, available inputs, authorized scope and
  recovery expectations in its instructions, and tell it to pass them to its
  own workers. It must report blockers to you instead of waiting for a person.

## Plan

1. Read your journal first with `sys_journal_read` and interpret the request
   against it. Resume an unfinished workflow when asked to continue it; add
   compatible new work or revisions while preserving completed results and
   unfinished steps. Follow an explicit request to replace the plan or start
   a fresh run. Don't ask what to do merely because unfinished steps exist.
   Choose a reversible ordering for competing work. A conflict justifies a
   question only under "Before asking or declaring a blocker". A finished
   plan does not block a new one.
   An explicit Stop still requires an explicit Continue for the stopped work.
2. Use the current run's plan if one is already in the journal. Otherwise,
   write the plan with `sys_journal_write_plan` before executing its steps. Name
   the plan for its goal in a few words ("Cut GET /orders p95 by 20%"). Give
   each step a short, stable ID (`profile`, `opt-pool`), a name, and its
   dependencies. A new step needs an ID the journal hasn't used yet, since a
   step's status is the newest one written under its ID.

   List the steps in the order you'll work on them. The Tasks tab numbers
   them in that order, so the user should see the work move steadily down the
   list. Put each step after the steps whose results it needs, and steps that
   can run at the same time next to each other. Steps that would change the
   same files, or measure on the same machine, can't run at the same time:
   make the later one depend on the earlier.
3. Include a discovery step when the run needs new information, such as which
   PRs to review or what causes today's failing tests. Name it for the result
   it must produce, and investigate deeply enough to make dependent work
   concrete. An already understood procedure does not need an initial
   exploration step. Use the current instructions while doing each task;
   rereading a known SOP is not itself a default workflow step.
4. Replan when discovery or changed instructions affect the work. Preserve
   completed results and add concrete steps, with dependencies only on the
   results they need. Do not repeat research already completed while authoring
   the saved workflow unless new evidence makes it necessary.
5. Start once the goal is clear enough to make progress. Discover missing
   run inputs as needed and record reasonable assumptions. If a question meets
   "Before asking or declaring a blocker", ask it on a ready step with
   `sys_workflow_request_input`; don't turn routine uncertainty into a
   plan-review pause. In an unattended run, follow the automation rules.

## Run a saved workflow

When the user names a saved workflow ("Run the saved workflow "Triage flaky
tests" (ID …)", or "run my triage workflow" in chat):

- Do the plan's step 1 first: decide whether this resumes, extends or starts
  a run from the request and journal.
- A session started as a run of the workflow, from its Run button or by
  another session, already has its starting plan in the journal. Continue
  that run using its saved snapshot and step IDs; do not write another
  starting plan with `workflow_id`. Later edits to the saved workflow do not
  replace this run's plan. Load the workflow only if you need its
  description. An explicit request to run it again starts a new run.
- If there is no starting plan, load it with `sys_workflow_load`, by its ID
  when you have one, otherwise by name. If they describe it without its exact
  name, or the load returns
  `workflow_not_found`, list their workflows with `sys_workflow_list` and pick
  the one whose description and context clearly fit. If the intended workflow
  still cannot be identified, follow "Before asking or declaring a blocker";
  don't choose an unrelated workflow just to avoid a question.
- If the run has no starting plan yet, its steps are your plan: write them
  with `sys_journal_write_plan`, named after the workflow, keeping their IDs
  and dependencies, and with
  `workflow_id` set to its ID, so the workflow's page lists this run. Put
  them in the order you'll work on them (see Plan), even if the saved order
  differs. Rename an ID only if this session's journal already used it. Fill
  the run's details into the step names and goals.
- Its description says what a run needs. Resolve missing details from the
  conversation, journal, current workspace and available tools, then use
  documented or reversible defaults. Any remaining missing input must meet
  "Before asking or declaring a blocker"; in an unattended run, follow the
  automation rules.
- The saved steps are a starting point: replan as the work needs, as with any
  plan. A replan leaves out `workflow_id`, since the run already started.

## Review the plan

When the user asks to review the plan before the work starts:

- Add a review step (`review`, or a new ID if that ID was used earlier) before
  the work requiring approval. Any read-only discovery needed to form the plan
  comes before it; when the plan is already concrete, review can be first.
  Make the work requiring approval depend on the review.
- Once the plan is concrete, ask the user to approve it with
  `sys_workflow_request_input` on the review. Start none of the later steps
  until they approve, then write `done` on the review.
- If they ask for changes, write `started` on the review first, investigate
  what the revision requires, update the affected steps and ask again.

## Work through it

- Track the work in the plan only. Don't also keep your own task list (such as
  TodoWrite, TaskCreate or update_plan): the user sees the plan, and a second
  list would only disagree with it.
- Write `started` with `sys_journal_write_status` when you begin a step yourself.
- Start a step only once the steps it depends on are done: the journal refuses
  any status on it otherwise, even a question with `sys_workflow_request_input`
  (`step_not_ready`). If another step's work covered one, write `done` on it
  and say which step covered it. If one turned out unnecessary, replan without
  it.
- Run ready steps that don't depend on each other at the same time when you
  can, such as on sub-agents if you have them, and work on other ready steps
  while one waits on something slow, such as CI or another session.
- Write `done` when the step achieved its goal. Only `done` lets later steps
  start.
- Don't write `done` while a later step still needs something that's on its
  way, such as a PR another session will open, CI on your push or a deploy
  you started, even if the step's own goal is met. Keep the step `started`,
  say what it waits for in its status ("Sync started; waiting for its PR"),
  and check on it with a background timer, as for a session you start (see
  "Running another workflow as a step"). Write `done` once it arrives.
- Keep going while any step is ready or started. Don't end your turn until
  everything left is done, failed, waiting for the user, running in another
  session, or waits on one of those, unless the user or your instructions say
  to stop sooner.
- When a step meets "Before asking or declaring a blocker", first finish the
  ready steps that don't depend on the answer, since asking pauses your turn.
  Then ask with `sys_workflow_request_input`: the step, and everything you need
  in one call, each question with your options and your recommendation. It
  marks the step `needs_input`, puts the questions in the user's Inbox, and
  waits a few minutes for the answer. If it returns `still_open`, end your
  turn once nothing else is ready; the answer arrives later as a message. Use
  it, not your own question tool (such as AskUserQuestion or
  request_user_input).
- When the user answers a step that waits for them, in the Inbox, in the chat
  or with **Continue** in the Tasks tab, write `started` on it before you work
  on it again, or `done` if its work is already done, for example by another
  step. Don't leave a step you acted on waiting for the user.
- If the user asks you to stop, follow "Stop and resume" below. Do not ask
  for confirmation or create a workflow question just to pause.
- When a step stalls, inspect the failure, fix problems within scope and use
  an equivalent authorized fallback when available. Retry transient failures
  with a bounded retry or time budget appropriate to the operation. Before
  repeating an external action, check whether it already succeeded so a
  retry cannot duplicate it. Don't skip required checks or broaden permissions
  to get past a failure.
- Keep a step `started` while an external job or review is pending and arrange
  a completion notification or background check. A slow service or exhausted
  retry budget alone is not a reason to ask the user what to do. If recovery
  is exhausted and no work is still progressing, write `failed` with the
  evidence and continue independent steps. A question must meet "Before
  asking or declaring a blocker", and the run must be attended.
- Write `failed` when a step's own work failed. State the cause and what would
  allow a future retry. Don't mark blocked dependents done or remove required
  dependencies to pretend the workflow completed; report what remains blocked.
- When you resume, check your `needs_input` steps, the steps running in other
  sessions and the steps waiting for something on its way first, and write a
  new status for any that can go ahead.
- Replan with `sys_journal_write_plan` whenever the goal grows or changes. Keep
  step IDs stable across replans, so finished steps keep their status; give a
  step a new ID only when its work changes. Keep the plan's name while the
  goal stays the same, and change it when the goal changes. Put a new step
  where it belongs in the order of the work (see Plan), even if that's above
  steps already in the plan.
- Write a note with `sys_journal_write_note` before any external side effect
  (push, merge, tag, deploy), and look for those notes when you resume.
- If an explicit approval gate remains unsatisfied after checking the
  existing authorization, cite it and make that approval a step of its own.
  Finish the preparation and checks first so the decision concerns a concrete
  result, then use `sys_workflow_request_input`. Write `done` if approved, or
  `failed` if rejected. In an unattended run, record the missing approval as
  a failure with the evidence required above. Saving a plan alone does not
  authorize running it; a request to run it supplies that instruction within
  the requested scope.
- Keep entries short, and point to files, PRs and sessions instead of pasting
  them.

## Stop and resume

The workflow's Stop and Continue controls send you instructions. Stopping your
own turn is not enough: delegated work can keep running independently.

- Before launching background work, retain its owning session, task/terminal
  handle and a way to cancel it. With Codex `exec_command`, use `tty=true` for
  long-running commands so `write_stdin` can send Ctrl+C later. Record the
  handle in the journal; a PID alone may belong to a private process sandbox.
- On Stop, stop starting new work and cancel this run's timers, retries and
  queued follow-ups that could restart it. Scope the stop to this workflow
  run; leave unrelated work and the automation's recurring schedule alone.
- Tell every subagent and other Omnigent session working on this run to stop
  its own background tasks, agents and sessions recursively. Use the harness's
  agent/task controls, `sys_session_send` for your child sessions and
  `sys_session_message` for independent sessions. Send the recursive stop
  request before terminating a worker or the call waiting for it, so it can
  stop its descendants too. Stop the background work you own as well.
- Keep this main session available to coordinate cleanup. Verify that the
  workers and their descendants stopped; a queued message is not confirmation.
  Have each worker cancel its background commands through their original
  handles and wait for their exit results before reporting back. A closed
  stdin, missing handle, interrupted wait, or empty process list in another
  sandbox is not proof of shutdown. Report any such work as unverified until
  its owning task or host confirms it exited. If another session requested
  your stop, report the result to it even though your plan is unfinished.
  Report any work you could not stop. Record worker/session IDs, completed
  results and what remains in a journal note before ending your turn.
- Leave unfinished steps that were started as `started`, and unstarted steps
  unstarted. A user stop is neither success nor failure. Wait for an explicit
  Continue without asking another question or restarting on a timer.
- On Continue, Run or Retry, read the journal and check each worker's current
  state. Resume or restart previously stopped background tasks, subagents and
  other Omnigent sessions needed for the remaining work. Tell each worker to
  do the same recursively for its required descendants. Reuse live workers,
  preserve completed results and avoid duplicate work. If a worker cannot
  resume, start a replacement with the recorded context or do its remaining
  work here. Restore the monitoring needed to finish the workflow.

## Running another workflow as a step

When a step is a workflow of its own, such as getting one PR ready to merge,
run it in its own session:

- Write `started` on the step, then start a session for it with
  `sys_session_create`, using your own agent and `placement: same_host`.
  For a saved workflow, also pass its ID as `workflow_id`: the session starts
  with that workflow's plan, and the workflow's page lists the run.
  `sys_session_get_info` with no arguments gives your session ID, and
  `sys_agent_get` on that session gives your agent's ID. The first message
  must:
  - tell it to load the `workflow` skill and run the workflow, giving a saved
    workflow's name and ID
  - give it everything it needs, since it doesn't see this conversation
  - include the relevant decisions, existing authorization and limits, and
    whether this is an unattended run; tell it to resolve routine choices
    itself and report any remaining blockers to you, passing this context
    to its own workers
  - say that this session (give its ID) started it for the step (give the
    step ID), and that it must report back when its plan finishes
- If you can't start one, do the step's work in this session instead.
- Check on it periodically. Start a background timer with your own tools,
  such as a background shell that sleeps 5 minutes. Each time it fires, read
  the other session's journal with `sys_journal_read`, update the step, and
  start the timer again while that session is still working.
- When its plan finishes or it reports back, verify the result and write
  `done` on the step if every required task is done. For failures or an
  unexpected stop with tasks left, read its journal and result: supply missing
  context, resume or replace the worker, or finish the work here within the
  original scope. Check for work still running before replacing it to avoid
  duplicates. After recovery is exhausted, write `failed` naming the
  unresolved tasks. Preserve an intentional user Stop.
- Resolve a worker's questions from the context and authority you already
  have; do not grant permissions the user has not granted. Check a worker's
  claimed blocker against "Before asking or declaring a blocker" and send it
  a decision or missing context whenever possible. Consolidate any remaining
  human question in the coordinating session. For unattended runs, record
  only substantiated, unrecoverable blockers as `failed` and keep independent
  work moving; never leave a worker waiting for human input.

## Measurable goals

For a goal you can measure, such as "cut p95 latency by 20%":

- Measure a baseline first, as its own step.
- Make each candidate change its own step, and add new candidates by
  replanning as you learn.
- Keep tried steps in the plan, failed ones too, so the record shows what you
  tried.
- Make the goal step depend on the candidates you keep. When a try fails,
  replan so the goal no longer waits on it; the failed try stays in the plan.
- Record each measurement in the status text or a note.

## Finish

Before your final reply, write a status for every step you worked on. In the
reply, lead with anything the user needs to decide, then summarize what was
done and what is left. If another session started this workflow and your plan
is finished, report back to it now (see "When another workflow started
you").

To reuse a workflow later, the user can save it: the save-workflow skill does
that, and so does Save as workflow in the Tasks tab's ⋯ menu.
