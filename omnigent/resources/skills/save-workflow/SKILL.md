---
name: save-workflow
description: Save this session's plan, or a workflow the user describes, as one of their reusable workflows, so they can run it again from Run a workflow or by asking for it by name.
---

# Save a workflow

The user wants to reuse a workflow: this session's plan, or one they describe.
Save it with `sys_workflow_save`. If you don't have that tool, tell the user
this agent can't save workflows and stop.

## Name

Use the name the user picked: Save as workflow in the Tasks tab sends it along
with this skill. If they gave none, suggest a short name for what the workflow
does ("Triage flaky tests") in your reply, and save once they agree.

## Steps

- From this session: read the journal with `sys_journal_read`, `kind: "plan"`
  for the plan's graph, then its statuses and notes. Keep the steps worth
  repeating, and drop one-off steps and tries that were abandoned.
- From a description: read the repo if it helps, and draft the steps you'd
  plan for it, without doing the work. Write the draft as the session's plan
  with `sys_journal_write_plan`, named for the workflow, so the user sees it in
  the Tasks tab, and don't start its steps.
- Keep the exploration step first, written for the next run ("Explore the
  failing tests"). Leave out the review step: a run adds one when the user asks
  to review its plan.
- Keep step IDs short and stable, and the dependencies as they ran.
- List the steps in the order a run works on them: each right after the steps
  it depends on, so a step that needs only the exploration comes right after
  the exploration.

## Make it generic

- A detail that would differ next run comes out of the steps: the repo, the
  PR, the module or file, a ticket, a date. Name steps for the kind of work
  ("Fix the failing tests"), not this run's instance ("Fix
  test_upload_retry").
- A detail that stays the same stays: commands, conventions, checks.
- If the user wants it exact, keep the specifics.

## Description

Say what the workflow does in a sentence or two, then what a run needs: "Give
it: the test suite and the flaky test's name." A run supplies those details,
and the agent running it asks for any that are missing.

## Save

- A plan this session ran: save it now, with `sys_workflow_save`.
- A workflow drafted from a description waits for the user's go-ahead. Reply
  with what it does and what a run needs, point to its steps in the Tasks tab,
  and end with both choices: "Save it now, or try it once first: tell me what
  to run it on." Revise it as they ask by writing the plan again, and save its
  steps when they say so. To try it first, get the run's details, say what the
  trial will touch, and work through the plan with the workflow skill: it's
  already the plan, so don't plan it again. When the run is done, offer to save
  it, with anything the trial taught you.

On `name_taken`, ask the user for another name. After saving, reply with what
you saved, its name, description and steps, and say where to run it: Run a
workflow in the + menu of a session's composer, or Run in the workflow's ⋯
menu on the Workflows page in the sidebar.
