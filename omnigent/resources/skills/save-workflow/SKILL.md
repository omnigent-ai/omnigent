---
name: save-workflow
description: Design, revise, or save a reusable workflow from a description or this session's plan. Use for planning chats on the workflow page and requests to author a workflow without executing it.
---

# Author a reusable workflow

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

## Research the procedure while authoring

Before publishing a proposal, read the referenced SOPs, runbooks and relevant
project instructions. Follow links to procedures and skills far enough to
establish the actions, prerequisites, validation and approval boundaries that
determine the plan. This is planning work: inspect sources without carrying out
the workflow's operations.

Reuse relevant research already completed in the session. Refresh the affected
sources when changes to the request or new evidence warrant it.

Turn those findings into concrete step goals, dependencies and completion
criteria. Keep useful source references with the relevant steps so a later run
can use them without access to this planning conversation. If an essential
source is unavailable, identify that gap and draft the parts you can support;
do not hide an unknown procedure behind a future "explore the SOP" step.

Include discovery in the saved plan only when its result must be obtained for
each run, such as selecting current PRs, finding today's failures or resolving
the source revision to import. Name it for that result. Learning the procedure
and deciding the workflow's structure belong here, before saving; they are not
steps to repeat in every run. Do not freeze today's run inputs into the plan.

## Name

Use the name the user picked: Save as workflow in the Tasks tab sends it along
with this skill. If they gave none, choose a short name for what the workflow
does ("Triage flaky tests") and include it in the result without waiting for
agreement.

## Steps

- From this session: read the journal with `sys_journal_read`, `kind: "plan"`
  for the plan's graph, then its statuses and notes. Keep the steps worth
  repeating, and drop one-off steps and tries that were abandoned.
- From a description: research the procedure as above, then draft its execution
  steps. Write the draft as the session's plan with `sys_journal_write_plan`,
  named for the workflow, so the user sees it in the Tasks tab, and don't start
  its steps.
- Drop completed authoring research when saving a session's plan. Keep discovery
  that a future run needs, expressed in terms of its inputs and result. Leave
  out a review of the plan itself: a run adds one when the user requests it.
- Write steps the agent can carry through itself: discover inputs, do the
  work, verify the result and recover from ordinary failures. Include input
  discovery, defaults and bounded recovery in step goals where needed.
  Do not add human checkpoints for routine choices, checks or continuation.
  Preserve explicit approval requirements, naming their source and when they
  apply. Do not add a generic "ask if unsure" or "confirm before proceeding"
  step; specify the input lookup, default or recovery the agent should use.
- Keep retained step IDs stable. When dropping steps, reconnect dependencies
  to preserve needed prerequisites and remove references to dropped IDs.
- List steps after their prerequisites. A dependency must supply a result or
  satisfy a real gate needed by that step; do not put every step behind generic
  exploration or a human decision that does not affect it.
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
