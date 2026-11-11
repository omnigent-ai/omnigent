// The message that runs Omnigent's `workflow` skill from the "Run a workflow"
// dialog, in the session composer and on the new-session page.

/** The framework skill that plans and runs a workflow (`/workflow <goal>`). */
export const WORKFLOW_SKILL = "workflow";

/** Added to the workflow message when the user reviews the plan before work starts. */
export const REVIEW_PLAN_FIRST =
  "Make the plan's first step a review of the plan, and make every other step depend on it. " +
  "Ask me to approve the plan before you start anything else; if I ask for changes, replan " +
  "and ask again.";

/** Whether a skill list offers the workflow skill, so the composer can offer "Run a workflow". */
export function offersWorkflow(skills: readonly { name: string }[]): boolean {
  return skills.some((skill) => skill.name === WORKFLOW_SKILL);
}

/**
 * The message that runs the workflow skill for *goal*.
 *
 * @param skillPrefix The harness's skill prefix, `"/"` or `"$"` for Codex's own CLI.
 * @param goal What the user wants done, e.g. `"Add a --json flag"`.
 * @param reviewPlanFirst Whether the plan starts with a step that waits for the user's approval.
 * @returns e.g. `"/workflow Add a --json flag"`, plus {@link REVIEW_PLAN_FIRST} as its own
 *   paragraph when *reviewPlanFirst* is set.
 */
export function workflowMessage(
  skillPrefix: string,
  goal: string,
  reviewPlanFirst: boolean,
): string {
  const command = `${skillPrefix}${WORKFLOW_SKILL} ${goal.trim()}`;
  return reviewPlanFirst ? `${command}\n\n${REVIEW_PLAN_FIRST}` : command;
}
