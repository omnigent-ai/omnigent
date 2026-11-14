---
name: workflow
description: Plan and run a multi-step workflow from a description or one of the user's saved workflows. Keeps the plan and every step's status in the session's journal, so people can follow along in the Tasks tab and the work resumes cleanly.
---

# Run a workflow

The user described the work after `/workflow`, or named one of their saved
workflows. Turn it into a plan, then work through the plan, recording where
each step stands as you go. The user watches
the plan's steps as tasks in the session's Tasks tab and can steer or stop you
in chat at any time.

You need the `sys_journal_*` tools and `sys_workflow_request_input`, and
`sys_workflow_load` and `sys_workflow_list` for a saved workflow. If you don't
have them, tell the user this agent can't run workflows and stop.

## When another workflow started you

Check this first. If your first message says another session started this
workflow, that session is waiting on you. When your plan finishes, with every
step done or failed, message that session with `sys_session_message`, saying
how it went and what's left. This is very important: don't skip it, even if
you also tell the user, since the other workflow can't go on without your
message.

## When an automation runs you

Check this too. If your message says no one is watching this run, as an
Omnigent automation's note does, no one can answer you or approve anything,
so nothing in the run may wait for a person:

- Skip the plan review, even if the message asks for one. Add no review step,
  and wait for no approval.
- Decide each question yourself, wherever this skill says to ask, including
  one the note says to defer: take the option you'd recommend, and say what
  you chose and why in the step's status. Don't use
  `sys_workflow_request_input`, and write no `needs_input`.
- If a step can't be done without a person, such as a sign-in only they can
  do, write `failed` on it saying what it needs, and go on with the steps that
  don't depend on it.
- A session you start for a step (see "Running another workflow as a step")
  can't ask anyone either: say in its first message that no one is watching
  this run.

## Plan

1. Read your journal first with `sys_journal_read`. If the session's plan has
   steps that aren't done or failed, ask the user what to do with the new
   workflow before you plan it. Ask with `sys_workflow_request_input` on the
   step you'd work on next, saying what's left ("The current workflow has 2
   tasks left. What should I do with the new one?"), with these options and
   your recommendation:
   - "Add to the current workflow": replan with the current steps, which keep
     their status, plus steps for the new work.
   - "Replace the current workflow": plan the new work alone, dropping the
     steps left in the current plan.
   - "Keep the current workflow": go on with the current plan, and leave the
     new work aside.

   If the plan still has the step you asked on, write `started` on it when you
   get to it. A plan whose steps are all done or failed is finished: plan the
   new work without asking.
2. Write the plan with `sys_journal_write_plan` before you start working. Name
   the plan for its goal in a few words ("Cut GET /orders p95 by 20%"). Give
   each step a short, stable ID (`profile`, `opt-pool`), a name, and its
   dependencies. A new step needs an ID the journal hasn't used yet, since a
   step's status is the newest one written under its ID.

   List the steps in the order you'll work on them. The Tasks tab numbers
   them in that order, so the user should see the work move steadily down the
   list. Put each step right after the steps it depends on: a step that needs
   only the exploration, such as a check, goes right after the exploration,
   and steps that can run at the same time go next to each other. Steps that
   would change the same files, or measure on the same machine, can't run at
   the same time: make the later one depend on the earlier.
3. Make the first step exploring the work: ID `explore` (`explore-2`,
   `explore-3` and on when an earlier workflow in this session used it), and a
   name that starts with "Explore" ("Explore the open PRs and pick 3"), so the
   user can tell it apart in the Tasks tab. Explore without changing anything,
   deeply enough that the steps you add from it are reasonably specific: each
   a concrete piece of work, such as one change or one PR, rather than a broad
   phase like "implement the changes". A small, clear task needs only a quick
   look.
4. Once you've explored, replan: keep the exploration first and add the work
   after it, depending on it. A single goal is one step after the exploration.
5. Ask first only if the description is ambiguous: write the plan with what
   you know, and ask with `sys_workflow_request_input` on its first step.
   Otherwise start.

## Run a saved workflow

When the user names a saved workflow ("Run the saved workflow "Triage flaky
tests" (ID …)", or "run my triage workflow" in chat):

- Do the plan's step 1 first: ask before you replace a plan with steps left.
- Load it with `sys_workflow_load`, by its ID when you have one, otherwise by
  name. If they describe it without its exact name, or the load returns
  `workflow_not_found`, list their workflows with `sys_workflow_list` and pick
  the one whose description fits. If more than one fits, or none does, ask
  the user which they meant, or whether to describe the work instead.
- Its steps are your plan: write them with `sys_journal_write_plan`, named
  after the workflow, keeping their IDs and dependencies, and with
  `workflow_id` set to its ID, so the workflow's page lists this run. Put
  them in the order you'll work on them (see Plan), even if the saved order
  differs. Rename an ID only if this session's journal already used it. Fill
  the run's details into the step names and goals.
- Its description says what a run needs. If the details the user gave leave
  something out, ask with `sys_workflow_request_input` on the first step.
- The saved steps are a starting point: replan as the work needs, as with any
  plan. A replan leaves out `workflow_id`, since the run already started.

## Review the plan

When the user asks to review the plan before the work starts:

- Make the review the second step (`review`, numbered like the exploration
  when an earlier workflow used it), depending on the exploration, and make
  every step you add after it depend on the review. Keep those two steps first
  in every version of the plan.
- Once you've explored and added the work, ask the user to approve the plan
  with `sys_workflow_request_input` on the review. Start none of the later
  steps until they approve, then write `done` on the review.
- If they ask for changes, write `started` on the review first, then restart
  the exploration to look into what they asked, update the steps after the
  review, and ask again.

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
- When a step needs the user (an approval or a decision), first finish the
  ready steps that don't depend on the answer, since asking pauses your turn.
  Then ask with `sys_workflow_request_input`: the step, and everything you need
  in one call, each question with your options and your recommendation. It
  marks the step `needs_input`, puts the questions in the user's Inbox, and
  waits for the answer. Use it, not your own question tool (such as
  AskUserQuestion or request_user_input).
- When the user answers a step that waits for them, in the Inbox, in the chat
  or with **Continue** in the Tasks tab, write `started` on it before you work
  on it again, or `done` if its work is already done, for example by another
  step. Don't leave a step you acted on waiting for the user.
- If the user asks you to stop and wait for them, write `needs_input` on every
  ready step, with text like "Paused at your request", then ask once whether
  to go on, with `sys_workflow_request_input` on the next of them.
- When something you can't clear yourself stops a step, such as CI being down
  or a review you're waiting on, the user decides how to go on. If it may clear
  by itself, such as a flaky check, retry once first. Then ask with
  `sys_workflow_request_input`, saying what's in the way and offering the ways
  forward you see (retry later, a workaround, or dropping the step), with your
  recommendation.
- Write `failed` when a step's own work failed. The text says why in a few
  words.
- If the user stops you, leave the step you were on `started`; the Tasks tab
  shows it as stopped while you're idle. A stop isn't the step failing, so
  don't write `failed` for it. Write a note if it helps to say what's left.
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
- Make an approval a step of its own, and ask for it with
  `sys_workflow_request_input` ("Approve the deploy to prod?"). Write `done` if
  approved, or `failed` if rejected.
- Keep entries short, and point to files, PRs and sessions instead of pasting
  them.

## Running another workflow as a step

When a step is a workflow of its own, such as getting one PR ready to merge,
run it in its own session:

- Write `started` on the step, then start a session for it with
  `sys_session_create`, using your own agent and `placement: same_host`.
  `sys_session_get_info` with no arguments gives your session ID, and
  `sys_agent_get` on that session gives your agent's ID. The first message
  must:
  - tell it to load the `workflow` skill and run the workflow
  - give it everything it needs, since it doesn't see this conversation
  - say that this session (give its ID) started it for the step (give the
    step ID), and that it must report back when its plan finishes
- If you can't start one, do the step's work in this session instead.
- Check on it periodically. Start a background timer with your own tools,
  such as a background shell that sleeps 5 minutes. Each time it fires, read
  the other session's journal with `sys_journal_read`, update the step, and
  start the timer again while that session is still working.
- When its plan finishes or it reports back, write `done` on the step if
  every task is done, or `failed` naming the tasks that failed. If it stopped
  with tasks left, ask the user how to go on with `sys_workflow_request_input`
  on the step, as for anything else you can't clear yourself. If it's waiting
  for the user, leave the step alone: the user answers in that session.

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
