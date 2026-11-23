// Reads back the message `savedWorkflowMessage` in ./workflow writes for New automation,
// without its plan review. Trigger templates use it too, so it imports nothing.

// Keep in sync with _SAVED_WORKFLOW_RUN in omnigent/server/scheduled/workflow_automations.py.
const SAVED_WORKFLOW_RUN =
  /^[/$]workflow Run the saved workflow "(.+)" \(ID ([0-9a-f]{32})\)\.(?:\n\n([\s\S]*))?$/;

// A run's first line, for instructions whose end the prompt doesn't mark. Keep in sync
// with _SAVED_WORKFLOW_RUN_START in omnigent/server/scheduled/workflow_automations.py.
const SAVED_WORKFLOW_RUN_OPENING =
  /^[/$]workflow Run the saved workflow "(.+)" \(ID ([0-9a-f]{32})\)\.(?:\n|$)/;

/** The saved workflow *text* opens with a run of, whatever follows that first line. */
export function savedWorkflowRunOpening(text: string): { id: string; name: string } | null {
  const match = SAVED_WORKFLOW_RUN_OPENING.exec(text.trimStart());
  return match ? { id: match[2], name: match[1] } : null;
}

// How any request to run a saved workflow opens, exact or not. Keep in sync with
// _SAVED_WORKFLOW_RUN_LIKE_OPENING in omnigent/server/scheduled/workflow_automations.py.
const SAVED_WORKFLOW_RUN_LIKE_OPENING = /^(?:[/$]workflow )?run the saved workflow/i;

/**
 * Whether *text* opens with `Run the saved workflow`, with or without the skill prefix, in
 * any letter case. The agent runs the workflow even when the rest isn't the exact message
 * {@link parseSavedWorkflowRun} reads, so such a run still needs its own session.
 */
export function opensLikeSavedWorkflowRun(text: string): boolean {
  return SAVED_WORKFLOW_RUN_LIKE_OPENING.test(text.trimStart());
}

/**
 * The saved workflow that *instructions* run, such as an automation's prompt or a
 * trigger's instructions for each session.
 *
 * @returns The workflow's ID and name and the run's details, or `null` for any other text.
 */
export function parseSavedWorkflowRun(
  instructions: string,
): { id: string; name: string; details: string } | null {
  const match = SAVED_WORKFLOW_RUN.exec(instructions.trim());
  if (!match) return null;
  return { id: match[2], name: match[1], details: (match[3] ?? "").trim() };
}
