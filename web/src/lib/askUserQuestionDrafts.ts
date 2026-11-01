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
const drafts = new Map<string, { signature: string; draft: AskUserQuestionDraft }>();

/**
 * Identity of a question list for draft matching: only the fields a draft's
 * answers depend on, so the structured payload and the parsed content
 * preview of the same prompt match despite key order or optional fields.
 */
export function askUserQuestionSignature(questions: ClaudeQuestion[]): string {
  return JSON.stringify(
    questions.map((question) => [
      question.id ?? "",
      question.question,
      Boolean(question.multiSelect),
      Boolean(question.isSecret),
      question.isOther !== false,
      question.textInput?.default ?? null,
      question.options.map((option) => option.label),
    ]),
  );
}

/** Saved draft for this card; only restored when the questions are unchanged. */
export function getAskUserQuestionDraft(
  elicitationId: string,
  signature: string,
): AskUserQuestionDraft | undefined {
  const entry = drafts.get(elicitationId);
  return entry?.signature === signature ? structuredClone(entry.draft) : undefined;
}

/** Remember a card's unsent answers; only the most recent drafts are kept. */
export function saveAskUserQuestionDraft(
  elicitationId: string,
  signature: string,
  draft: AskUserQuestionDraft,
): void {
  drafts.delete(elicitationId);
  drafts.set(elicitationId, { signature, draft });
  if (drafts.size > MAX_DRAFTS) drafts.delete(drafts.keys().next().value as string);
}

/**
 * Forget a card's draft once its prompt has been answered. With a
 * ``signature``, only a draft saved for those questions is dropped.
 */
export function clearAskUserQuestionDraft(elicitationId: string, signature?: string): void {
  if (signature === undefined || drafts.get(elicitationId)?.signature === signature) {
    drafts.delete(elicitationId);
  }
}
