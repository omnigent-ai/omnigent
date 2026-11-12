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
- From a description: draft the steps you'd plan for it, without doing the
  work.
- Keep the exploration step first, written for the next run ("Explore the
  failing tests"). Leave out the review step: a run adds one when the user asks
  to review its plan.
- Keep step IDs short and stable, and the dependencies as they ran.

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

Call `sys_workflow_save` with the name, the description and the steps. On
`name_taken`, ask the user for another name. Then reply with what you saved,
its name, description and steps, and say it's now under Run a workflow.
