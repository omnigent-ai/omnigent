const SESSION_DRAFTS_KEY = "omnigent.sessionDrafts";
export const SESSION_TEXT_DRAFT_EVENT = "omnigent:session-text-draft";

interface SessionDraft {
  text: string;
  files: File[];
  notice?: string;
}

function loadDraftsFromStorage(): Map<string, SessionDraft> {
  try {
    const raw = window.sessionStorage.getItem(SESSION_DRAFTS_KEY);
    if (!raw) return new Map();
    const stored = JSON.parse(raw) as Record<string, string>;
    return new Map(
      Object.entries(stored)
        .filter(([, text]) => Boolean(text))
        .map(([id, text]) => [id, { text, files: [] }]),
    );
  } catch {
    return new Map();
  }
}

export const sessionDrafts = loadDraftsFromStorage();

export function saveDraftsToStorage(drafts: Map<string, SessionDraft>): void {
  try {
    const stored = Object.fromEntries(
      [...drafts].filter(([, draft]) => Boolean(draft.text)).map(([id, draft]) => [id, draft.text]),
    );
    if (Object.keys(stored).length > 0) {
      window.sessionStorage.setItem(SESSION_DRAFTS_KEY, JSON.stringify(stored));
    } else {
      window.sessionStorage.removeItem(SESSION_DRAFTS_KEY);
    }
  } catch {
    // In-memory drafts still work when storage is unavailable.
  }
}

export function setSessionTextDraft(sessionId: string, text: string, notice?: string): void {
  if (text) sessionDrafts.set(sessionId, { text, files: [], notice });
  else sessionDrafts.delete(sessionId);
  saveDraftsToStorage(sessionDrafts);
  window.dispatchEvent(
    new CustomEvent(SESSION_TEXT_DRAFT_EVENT, { detail: { sessionId, text, notice } }),
  );
}
