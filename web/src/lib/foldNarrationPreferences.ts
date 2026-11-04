// Per-device opt-in to fold a settled turn's progress messages into its "Worked" row
// for agents without an answer label. Codex labels its answer and folds regardless; for
// other agents the last message is only a heuristic for the answer.

import { createStoredChoicePreference } from "./storedChoicePreference";

export const FOLD_ASSISTANT_NARRATION_STORAGE_KEY = "omnigent:fold-assistant-narration";

export const DEFAULT_FOLD_ASSISTANT_NARRATION = false;

const preference = createStoredChoicePreference(
  FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
  ["false", "true"],
  DEFAULT_FOLD_ASSISTANT_NARRATION ? "true" : "false",
);

/** Read the preference; storage errors and unknown values mean the default. */
export function readFoldAssistantNarration(): boolean {
  return preference.read() === "true";
}

/** Persist the preference and notify chats already on screen. */
export function writeFoldAssistantNarration(enabled: boolean): void {
  preference.write(enabled ? "true" : "false");
}

/** Live value of the preference, including changes made in other windows. */
export function useFoldAssistantNarration(): boolean {
  return preference.use() === "true";
}
