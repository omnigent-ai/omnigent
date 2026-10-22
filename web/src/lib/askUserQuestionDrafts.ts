import type { ClaudeQuestion } from "./askUserQuestion";

/** Unsent answers on a pending AskUserQuestion card. */
export interface AskUserQuestionDraft {
  currentIndex: number;
  selections: Record<string, string | string[]>;
  customSelected: Record<string, boolean>;
  customInputs: Record<string, string>;
}

const MAX_DRAFTS = 20;

// In memory only, so typed answers never reach browser storage.
const drafts = new Map<string, { questions: string; draft: AskUserQuestionDraft }>();

/** Saved draft for this card; only restored when the questions are unchanged. */
export function getAskUserQuestionDraft(
  elicitationId: string,
  questions: ClaudeQuestion[],
): AskUserQuestionDraft | undefined {
  const entry = drafts.get(elicitationId);
  return entry?.questions === JSON.stringify(questions) ? structuredClone(entry.draft) : undefined;
}

/** Remember a card's unsent answers; only the most recent drafts are kept. */
export function saveAskUserQuestionDraft(
  elicitationId: string,
  questions: ClaudeQuestion[],
  draft: AskUserQuestionDraft,
): void {
  drafts.delete(elicitationId);
  drafts.set(elicitationId, { questions: JSON.stringify(questions), draft });
  if (drafts.size > MAX_DRAFTS) drafts.delete(drafts.keys().next().value as string);
}

/** Forget a card's draft once its prompt has been answered. */
export function clearAskUserQuestionDraft(elicitationId: string): void {
  drafts.delete(elicitationId);
}
