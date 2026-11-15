---
name: save-workflow
description: Save this session's plan, or a workflow the user describes, as one of their reusable workflows, so they can run it again from Run a workflow or by asking for it by name.
---

# Save a workflow

The user wants to reuse a workflow: this session's plan, or one they describe.
Use `sys_workflow_save` when the request calls for saving. If it is unavailable,
still prepare the plan with the available tools and report that it could not
be saved.

## Work from the context

Use the request, conversation, journal and relevant project context to make a
useful workflow without an interview. Choose ordinary names, step boundaries
and reversible defaults yourself, and briefly state assumptions. Ask about the
objective only if neither the request nor that context identifies what the
workflow should accomplish. Explain what you checked and the specific missing
objective. Otherwise produce a useful plan with assumptions and run inputs.
Missing credentials, run targets or execution approvals belong in the plan's
requirements; they do not prevent authoring or saving it.

If an explicit review instruction or a tool permission prevents saving,
prepare the proposal first, then cite the exact instruction or observed
denial. Do not invent an approval requirement because a workflow will later
perform external actions. When unattended, resolve questions with the
coordinating session if there is one. If a concrete blocker remains, report
what you prepared, what you checked and exactly what prevents completion;
do not wait for a person.

A request to create or save a workflow authorizes saving it, not executing its
steps. Respect an explicit request to draft, review or try it before saving.
In a workflow page's planning chat, follow its instructions: publish the
proposal in the journal and let **Save plan** apply it to the existing
workflow; do not create another workflow or run its steps.

## Name

Use the name the user picked: Save as workflow in the Tasks tab sends it along
with this skill. If they gave none, choose a short name for what the workflow
does ("Triage flaky tests") and include it in the result without waiting for
agreement.

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
- Write steps the agent can carry through itself: discover inputs, do the
  work, verify the result and recover from ordinary failures. Include input
  discovery, defaults and bounded recovery in step goals where needed.
  Do not add human checkpoints for routine choices, checks or continuation.
  Preserve explicit approval requirements, naming their source and when they
  apply. Do not add a generic "ask if unsure" or "confirm before proceeding"
  step; specify the input lookup, default or recovery the agent should use.
- Keep step IDs short and stable, and the dependencies as they ran.
- List the steps in the order a run works on them: each right after the steps
  it depends on, so a step that needs only the exploration comes right after
  the exploration.
- End with a single final step, such as "Report the results", that depends on
  every step that would otherwise be last, so a run ends in one place instead
  of in several steps side by side. If the plan you're saving ends in several
  steps, add one.

## Make it generic

- A detail that would differ next run comes out of the steps: the repo, the
  PR, the module or file, a ticket, a date. Name steps for the kind of work
  ("Fix the failing tests"), not this run's instance ("Fix
  test_upload_retry").
- A detail that stays the same stays: commands, conventions, checks.
- If the user wants it exact, keep the specifics.

## Description

Say what the workflow does in a sentence or two, then how a run finds its
inputs and which ones it actually needs. For example: "Find and fix flaky
tests in the current repository. Use its test configuration unless a suite
is specified." Separate required inputs from optional overrides, and name
any authorization needed for external actions. The running agent should
look up missing details and apply documented defaults before asking.

## Save

- When asked to create or save a workflow, save it with `sys_workflow_save`
  once the steps are ready, whether they came from a run or a description.
  Do not ask for the same go-ahead again.
- If the user requested a draft or review first, show the proposal and wait
  only for that requested review. Revise it with `sys_journal_write_plan` as
  they ask. If they requested a trial, use the workflow skill to run the
  existing plan with the available context and inputs; don't plan it again.
  Save afterward if that was already requested, incorporating what the trial
  taught you.

On `name_taken`, load the existing workflow if possible. If it already matches
the requested workflow, reuse it and report its name and ID. Otherwise choose
a descriptive, distinct name and retry, reporting the chosen name; ask only
if the user required that exact name. Do not overwrite an existing workflow
or keep retrying the same collision.

After saving, reply with its name, description, relevant assumptions and where
to run it: Run a workflow in the + menu of a session's composer, or Run in the
workflow's ⋯ menu on the Workflows page in the sidebar.
