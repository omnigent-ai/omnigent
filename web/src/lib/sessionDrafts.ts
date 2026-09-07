import { useSyncExternalStore } from "react";

export interface SessionDraft {
  text: string;
  files: File[];
}

const SESSION_DRAFTS_KEY = "omnigent.sessionDrafts";
const listeners = new Set<() => void>();

function loadDraftsFromStorage(): Map<string, SessionDraft> {
  if (typeof window === "undefined") return new Map();
  try {
    const raw = window.sessionStorage.getItem(SESSION_DRAFTS_KEY);
    if (!raw) return new Map();
    const entries = JSON.parse(raw) as Record<string, string>;
    const drafts = new Map<string, SessionDraft>();
    for (const [id, text] of Object.entries(entries)) {
      if (text) drafts.set(id, { text, files: [] });
    }
    return drafts;
  } catch {
    return new Map();
  }
}

function saveDraftsToStorage(): void {
  if (typeof window === "undefined") return;
  try {
    const entries: Record<string, string> = {};
    for (const [id, draft] of sessionDrafts) {
      if (draft.text) entries[id] = draft.text;
    }
    if (Object.keys(entries).length === 0) {
      window.sessionStorage.removeItem(SESSION_DRAFTS_KEY);
    } else {
      window.sessionStorage.setItem(SESSION_DRAFTS_KEY, JSON.stringify(entries));
    }
  } catch {
    // Storage full or unavailable — drafts still work in-memory.
  }
}

const sessionDrafts = loadDraftsFromStorage();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notifyListeners(): void {
  for (const listener of listeners) listener();
}

export function getSessionDraft(conversationId: string): SessionDraft | undefined {
  return sessionDrafts.get(conversationId);
}

export function setSessionDraft(conversationId: string, draft: SessionDraft): void {
  if (draft.text === "" && draft.files.length === 0) {
    sessionDrafts.delete(conversationId);
  } else {
    sessionDrafts.set(conversationId, draft);
  }
  saveDraftsToStorage();
  notifyListeners();
}

export function hasSessionDraft(conversationId: string): boolean {
  const draft = sessionDrafts.get(conversationId);
  return draft !== undefined && (draft.text.trim() !== "" || draft.files.length > 0);
}

export function useHasSessionDraft(conversationId: string): boolean {
  return useSyncExternalStore(
    subscribe,
    () => hasSessionDraft(conversationId),
    () => false,
  );
}

/** Clear all drafts, primarily for logout/reset flows and isolated tests. */
export function clearSessionDrafts(): void {
  sessionDrafts.clear();
  saveDraftsToStorage();
  notifyListeners();
}
