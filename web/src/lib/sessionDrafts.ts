import { useSyncExternalStore } from "react";
import { readComposerDraft, type ComposerDraft } from "./replyDraft";

export interface SessionDraft extends ComposerDraft {
  files: File[];
}

const SESSION_DRAFTS_KEY = "omnigent.sessionDrafts";
const listeners = new Set<() => void>();
const retiredDraftIds = new Set<string>();

function loadDraftsFromStorage(): Map<string, SessionDraft> {
  if (typeof window === "undefined") return new Map();
  try {
    const raw = window.sessionStorage.getItem(SESSION_DRAFTS_KEY);
    if (!raw) return new Map();
    const entries: unknown = JSON.parse(raw);
    if (typeof entries !== "object" || entries === null || Array.isArray(entries)) return new Map();
    const drafts = new Map<string, SessionDraft>();
    for (const [id, entry] of Object.entries(entries)) {
      const draft = readComposerDraft(entry);
      if (draft?.text) drafts.set(id, { ...draft, files: [] });
    }
    return drafts;
  } catch {
    return new Map();
  }
}

function saveDraftsToStorage(): void {
  if (typeof window === "undefined") return;
  try {
    const entries: Record<string, string | ComposerDraft> = {};
    for (const [id, draft] of sessionDrafts) {
      if (draft.text)
        entries[id] = draft.replyDraft
          ? { text: draft.text, replyDraft: draft.replyDraft }
          : draft.text;
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
  if (retiredDraftIds.has(conversationId)) return;
  if (draft.text === "" && draft.files.length === 0) {
    sessionDrafts.delete(conversationId);
  } else {
    sessionDrafts.set(conversationId, draft);
  }
  saveDraftsToStorage();
  notifyListeners();
}

/** Remove a draft and ignore any late cleanup write for the retired id. */
export function retireSessionDraft(conversationId: string): SessionDraft | undefined {
  const draft = sessionDrafts.get(conversationId);
  retiredDraftIds.add(conversationId);
  if (draft === undefined) return undefined;
  sessionDrafts.delete(conversationId);
  saveDraftsToStorage();
  notifyListeners();
  return draft;
}

/** Merge a failed temporary session's unsent input back into its source draft. */
export function recoverFailedSessionDraft<T extends { message: string; files: File[] }>(
  originalDraft: T,
  temporaryConversationId?: string,
): T {
  if (temporaryConversationId === undefined) return originalDraft;
  const temporaryDraft = retireSessionDraft(temporaryConversationId);
  if (temporaryDraft === undefined) return originalDraft;
  const message = [originalDraft.message, temporaryDraft.text]
    .filter((part) => part.trim() !== "")
    .join("\n\n");
  return {
    ...originalDraft,
    message,
    files: [...originalDraft.files, ...temporaryDraft.files],
  };
}

/** Move an unsent draft when a temporary conversation receives its real id. */
export function promoteSessionDraft(
  temporaryConversationId: string,
  conversationId: string,
): SessionDraft | undefined {
  const draft = retireSessionDraft(temporaryConversationId);
  if (draft === undefined) return undefined;
  sessionDrafts.set(conversationId, draft);
  saveDraftsToStorage();
  notifyListeners();
  return draft;
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
  retiredDraftIds.clear();
  saveDraftsToStorage();
  notifyListeners();
}
