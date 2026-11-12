// The message that runs Omnigent's `workflow` skill from the "Run a workflow"
// dialog, in the session composer and on the new-session page.

/** The framework skill that plans and runs a workflow (`/workflow <description>`). */
export const WORKFLOW_SKILL = "workflow";

/**
 * Added to the workflow message when the user reviews the plan before work starts. The agent
 * explores first, so the plan the user approves is specific and rests on what it found.
 */
export const REVIEW_PLAN_FIRST =
  "Make the plan's first step exploring the work, without changing anything, deeply enough " +
  "that the plan you write from it is reasonably specific: each step a concrete piece of " +
  "work, such as one change or one PR, rather than a broad phase like implementing " +
  "everything. Make its second step my review of the plan, with every later step depending " +
  "on the review. Once you've explored, update the plan with what you found: drop the " +
  "exploration step, make my review the first step, and make every other step depend on it. " +
  "Then ask me to approve the plan before you start anything else; if I ask for changes, " +
  "replan and ask again.";

/** Whether a skill list offers the workflow skill, so the composer can offer "Run a workflow". */
export function offersWorkflow(skills: readonly { name: string }[]): boolean {
  return skills.some((skill) => skill.name === WORKFLOW_SKILL);
}

/**
 * The message that runs the workflow skill for *description*.
 *
 * @param skillPrefix The harness's skill prefix, `"/"` or `"$"` for Codex's own CLI.
 * @param description What the user wants done, e.g. `"Add a --json flag"`.
 * @param reviewPlanFirst Whether the agent explores, then waits for the user to approve its plan.
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
