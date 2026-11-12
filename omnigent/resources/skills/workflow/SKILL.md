---
name: workflow
description: Plan and run a multi-step workflow from a description. Keeps the plan and every step's status in the session's journal, so people can follow along in the Tasks tab and the work resumes cleanly.
---

# Run a workflow

The user described the work after `/workflow`. Turn it into a plan, then work
through the plan, recording where each step stands as you go. The user watches
the plan's steps as tasks in the session's Tasks tab and can steer or stop you
in chat at any time.

You need the `sys_journal_*` tools and `sys_workflow_request_input`. If you
don't have them, tell the user this agent can't run workflows and stop.

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
   step's status is the newest one written under its ID. A single goal is a
   one-step plan.
3. Ask about the description first only if it's ambiguous: write the plan with
   what you know, and ask with `sys_workflow_request_input` on its first step.
   Otherwise start.

## Work through it

- Track the work in the plan only. Don't also keep your own task list (such as
  TodoWrite, TaskCreate or update_plan): the user sees the plan, and a second
  list would only disagree with it.
- Write `started` with `sys_journal_write_status` when you begin a step yourself.
- Start a step only once the steps it depends on are done: the journal refuses
  `started` or `done` otherwise (`step_not_ready`). If another step's work
  covered one, write `done` on it and say which step covered it. If one turned
  out unnecessary, replan without it.
- Write `done` when the step achieved its goal. Only `done` lets later steps
  start.
- Keep going while any step is ready or started. Don't end your turn until
  everything left is done, failed, waiting for the user, or waits on one of
  those, unless the user or your instructions say to stop sooner.
- When a step needs the user (an approval or a decision), first finish the
  ready steps that don't depend on the answer, since asking pauses your turn.
  Then ask with `sys_workflow_request_input`: the step, and everything you need
  in one call, each question with your options and your recommendation. It
  marks the step `needs_input`, puts the questions in the user's Inbox, and
  waits for the answer. Use it, not your own question tool (such as
  AskUserQuestion or request_user_input). Write `started` when you resume the
  step after the answer.
- If the user asks you to stop and wait for them, write `needs_input` on every
  ready step, with text like "Paused at your request", then ask once whether
  to go on, with `sys_workflow_request_input` on the next of them.
- When something you can't clear yourself stops a step, such as CI being down
  or a review you're waiting on, the user decides how to go on. If it may clear
  by itself, such as a flaky check, retry once first. Then ask with
  `sys_workflow_request_input`, saying what's in the way and offering the ways
  forward you see (retry later, a workaround, or dropping the step), with your
  recommendation.
- In a run no one is watching, such as an automation's, don't ask: write
  `needs_input` with the options and your recommendation, in a note if they
  need more room, and go on with the steps that don't depend on it.
- Write `failed` when a step's own work failed. The text says why in a few
  words.
- If the user stops you, leave the step you were on `started`; the Tasks tab
  shows it as stopped while you're idle. A stop isn't the step failing, so
  don't write `failed` for it. Write a note if it helps to say what's left.
- When you resume, check your `needs_input` steps first, and write a new status
  for any that can go ahead.
- Replan with `sys_journal_write_plan` whenever the goal grows or changes. Keep
  step IDs stable across replans, so finished steps keep their status; give a
  step a new ID only when its work changes. Keep the plan's name while the
  goal stays the same, and change it when the goal changes.
- Write a note with `sys_journal_write_note` before any external side effect
  (push, merge, tag, deploy), and look for those notes when you resume.
- Make an approval a step of its own, and ask for it with
  `sys_workflow_request_input` ("Approve the deploy to prod?"). Write `done` if
  approved, or `failed` if rejected.
- Keep entries short, and point to files, PRs and sessions instead of pasting
  them.

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
done and what is left.
