// Per-device opt-in to fold a settled turn's progress messages into its "Worked" row
// for agents without an answer label. Codex labels its answer and folds regardless; for
// other agents the last message is only a heuristic for the answer.

import { createBooleanPreference } from "./booleanPreference";

export const FOLD_ASSISTANT_NARRATION_STORAGE_KEY = "omnigent:fold-assistant-narration";

export const DEFAULT_FOLD_ASSISTANT_NARRATION = false;

const preference = createBooleanPreference(
  FOLD_ASSISTANT_NARRATION_STORAGE_KEY,
  DEFAULT_FOLD_ASSISTANT_NARRATION,
);

export const readFoldAssistantNarration = preference.read;
export const writeFoldAssistantNarration = preference.write;
export const useFoldAssistantNarration = preference.use;
