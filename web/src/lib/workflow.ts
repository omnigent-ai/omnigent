// The message that runs Omnigent's `workflow` skill from the "Run a workflow"
// dialog, in the session composer and on the new-session page.

/** The framework skill that plans and runs a workflow (`/workflow <description>`). */
export const WORKFLOW_SKILL = "workflow";

/**
 * Added to the workflow message when the user reviews the plan before work starts. The
 * workflow skill says how: the review comes right after the exploration every plan starts with.
 */
export const REVIEW_PLAN_FIRST = "Let me review the plan before you start the work.";

/** Whether a skill list offers the workflow skill, so the composer can offer "Run a workflow". */
export function offersWorkflow(skills: readonly { name: string }[]): boolean {
  return skills.some((skill) => skill.name === WORKFLOW_SKILL);
}

/**
 * The message that runs the workflow skill for *description*.
 *
 * @param skillPrefix The harness's skill prefix, `"/"` or `"$"` for Codex's own CLI.
 * @param description What the user wants done, e.g. `"Add a --json flag"`.
 * @param reviewPlanFirst Whether the user reviews the plan before the work starts.
 * @returns e.g. `"/workflow Add a --json flag"`, plus {@link REVIEW_PLAN_FIRST} as its own
 *   paragraph when *reviewPlanFirst* is set.
 */
export function workflowMessage(
  skillPrefix: string,
  description: string,
  reviewPlanFirst: boolean,
): string {
  const command = `${skillPrefix}${WORKFLOW_SKILL} ${description.trim()}`;
  return reviewPlanFirst ? `${command}\n\n${REVIEW_PLAN_FIRST}` : command;
}

/** The framework skill that saves a session's plan as a reusable workflow. */
export const SAVE_WORKFLOW_SKILL = "save-workflow";

/** Hide workflow commands when the server has disabled their tools and APIs. */
export function filterWorkflowSkills<T extends { name: string }>(
  skills: readonly T[],
  enabled: boolean,
): readonly T[] {
  return enabled
    ? skills
    : skills.filter(({ name }) => name !== WORKFLOW_SKILL && name !== SAVE_WORKFLOW_SKILL);
}

/** Whether a skill list offers the save-workflow skill, so the Tasks tab can offer Save as workflow. */
export function offersSaveWorkflow(skills: readonly { name: string }[]): boolean {
  return skills.some((skill) => skill.name === SAVE_WORKFLOW_SKILL);
}

/**
 * The message that runs one of the user's saved workflows; the workflow skill
 * loads it by ID.
 *
 * @param skillPrefix The harness's skill prefix, `"/"` or `"$"` for Codex's own CLI.
 * @param workflow The saved workflow, e.g. `{ id: "0123…", name: "Triage flaky tests" }`.
 * @param details The specifics for this run, e.g. `"The upload tests"`; may be empty.
 * @param reviewPlanFirst Whether the user reviews the plan before the work starts.
 */
export function savedWorkflowMessage(
  skillPrefix: string,
  workflow: { id: string; name: string },
  details: string,
  reviewPlanFirst: boolean,
): string {
  const paragraphs = [`Run the saved workflow "${workflow.name}" (ID ${workflow.id}).`];
  if (details.trim()) paragraphs.push(details.trim());
  if (reviewPlanFirst) paragraphs.push(REVIEW_PLAN_FIRST);
  return `${skillPrefix}${WORKFLOW_SKILL} ${paragraphs.join("\n\n")}`;
}

/**
 * What a task's ⋯ menu asks of the agent: run a ready task, continue a stopped
 * one, go ahead with one waiting for you (`proceed`), or retry a failed one.
 */
export type TaskAction = "run" | "continue" | "proceed" | "retry";

const TASK_ACTION_VERBS: Record<Exclude<TaskAction, "proceed">, string> = {
  run: "Run",
  continue: "Continue",
  retry: "Retry",
};

/**
 * The message that asks the agent to run, continue, go ahead with or retry a
 * task, then go on with the rest of the plan rather than stop after that task.
 *
 * @param taskName The task's name, e.g. `"Review #6109"`.
 * @returns e.g. `Run the task "Share the review files", then go on with the rest of the plan.`
 */
export function taskActionMessage(action: TaskAction, taskName: string): string {
  if (action === "proceed") {
    return `It's fine to continue the task "${taskName}". Mark it started and go ahead with it (or mark it done if its work is already done), then go on with the rest of the plan.`;
  }
  return `${TASK_ACTION_VERBS[action]} the task "${taskName}", then go on with the rest of the plan.`;
}

/**
 * The message a plan's play button sends once the agent stopped with tasks
 * left: pick the plan back up and keep going, rather than do one task.
 *
 * @param planName The plan's name, e.g. `"Triage flaky tests"`, or null.
 * @returns e.g. `Continue the plan "Triage flaky tests" from where you left off, and work through the tasks that are left.`
 */
export function continuePlanMessage(planName: string | null): string {
  const plan = planName ? `the plan "${planName}"` : "the plan";
  return `Continue ${plan} from where you left off, and work through the tasks that are left.`;
}

/**
 * The message that saves the session's plan as a workflow named *name*.
 *
 * @param skillPrefix The harness's skill prefix, `"/"` or `"$"` for Codex's own CLI.
 * @param name The name the user picked, e.g. `"Triage flaky tests"`.
 */
export function saveWorkflowMessage(skillPrefix: string, name: string): string {
  return `${skillPrefix}${SAVE_WORKFLOW_SKILL} Save this session's plan as a workflow named "${name.trim()}".`;
}

/**
 * The first message of a session that drafts a new workflow from a description.
 * It names the save-workflow skill rather than invoking it: the agent, and with
 * it the skill prefix, can be changed after this is written.
 *
 * @param name The name the user picked, e.g. `"Triage flaky tests"`.
 * @param description What the workflow should do, e.g. `"Rerun the suite and file issues"`.
 */
export function createWorkflowPrompt(name: string, description: string): string {
  return `Use the ${SAVE_WORKFLOW_SKILL} skill to create a workflow named "${name.trim()}" from this description, without doing the work:\n\n${description.trim()}`;
}

/** The planning chat proposes a graph; Save plan in the editor applies it. */
export function editWorkflowPlanPrompt(workflow: {
  id: string;
  name: string;
  description: string;
}): string {
  return [
    `Help me design the reusable workflow "${workflow.name}" (ID ${workflow.id}).`,
    `Workflow description: ${workflow.description}`,
    "This is a planning conversation. Load the existing workflow with sys_workflow_load. Explore context if useful, but do not execute the workflow or make changes to the project.",
    "Use sys_journal_write_plan to publish the proposed steps, and write a new plan whenever I ask for revisions. Keep step IDs stable. Do not set workflow_id on this journal plan: this conversation is for planning, not an execution run.",
    "The workflow already exists. Do not call sys_workflow_save or create another workflow. I will use Save plan on the workflow page to apply the proposal. Draft a useful first proposal now, explain assumptions briefly, and iterate with me here.",
  ].join("\n\n");
}

/**
 * The first message of a session that runs one of the user's saved workflows.
 * Like {@link createWorkflowPrompt}, it names the skill rather than invoking it.
 *
 * @param workflow The saved workflow, e.g. `{ id: "0123…", name: "Triage flaky tests" }`.
 * @param details The user's additional instructions for this run, e.g.
 *   `"Only the tickets in PROJ-12"`, added as their own paragraph; may be empty.
 */
export function runSavedWorkflowPrompt(
  workflow: { id: string; name: string },
  details = "",
): string {
  const request = `Use the ${WORKFLOW_SKILL} skill to run the saved workflow "${workflow.name}" (ID ${workflow.id}).`;
  return details.trim() ? `${request}\n\n${details.trim()}` : request;
}

// A savedWorkflowMessage without the plan review, as New automation stores it.
const SAVED_WORKFLOW_RUN =
  /^[/$]workflow Run the saved workflow "(.+)" \(ID ([0-9a-f]{32})\)\.(?:\n\n([\s\S]*))?$/;

/**
 * Read back a {@link savedWorkflowMessage}, e.g. an automation's prompt.
 *
 * @returns The workflow's ID and name and the run's details, or `null` for any other text.
 */
export function parseSavedWorkflowMessage(
  text: string,
): { id: string; name: string; details: string } | null {
  const match = SAVED_WORKFLOW_RUN.exec(text.trim());
  if (!match) return null;
  return { id: match[2], name: match[1], details: (match[3] ?? "").trim() };
}
