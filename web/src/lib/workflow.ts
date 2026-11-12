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
 * It names the save-workflow skill rather than invoking it: the new-session page
 * picks the agent, and with it the skill prefix, after this is written.
 *
 * @param name The name the user picked, e.g. `"Triage flaky tests"`.
 * @param description What the workflow should do, e.g. `"Rerun the suite and file issues"`.
 */
export function createWorkflowPrompt(name: string, description: string): string {
  return `Use the ${SAVE_WORKFLOW_SKILL} skill to create a workflow named "${name.trim()}" from this description, without doing the work:\n\n${description.trim()}`;
}
