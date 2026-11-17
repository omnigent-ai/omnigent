// Reads back the message `savedWorkflowMessage` in ./workflow writes for New automation,
// without its plan review. Trigger templates use it too, so it imports nothing.

// Keep in sync with _SAVED_WORKFLOW_RUN in omnigent/server/scheduled/workflow_automations.py.
const SAVED_WORKFLOW_RUN =
  /^[/$]workflow Run the saved workflow "(.+)" \(ID ([0-9a-f]{32})\)\.(?:\n\n([\s\S]*))?$/;

// A run's first line, for instructions whose end the prompt doesn't mark. Keep in sync
// with _SAVED_WORKFLOW_RUN_START in omnigent/server/scheduled/workflow_automations.py.
const SAVED_WORKFLOW_RUN_OPENING =
  /^[/$]workflow Run the saved workflow "(.+)" \(ID ([0-9a-f]{32})\)\.(?:\n|$)/;

/** Whether *text* opens with a saved workflow's run, whatever follows that first line. */
export function opensWithSavedWorkflowRun(text: string): boolean {
  return SAVED_WORKFLOW_RUN_OPENING.test(text.trimStart());
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
