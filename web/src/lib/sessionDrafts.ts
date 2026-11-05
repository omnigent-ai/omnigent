import { useSyncExternalStore } from "react";
import { readComposerDraft, readStoredIds, type ComposerDraft } from "./replyDraft";

export interface SessionDraft extends ComposerDraft {
  files: File[];
  /** Failed startup inputs handed back into this draft, so a reload doesn't add them again. */
  startupInputIds?: string[];
  /** Stable ids of sends whose text this draft holds, until a read names their failed inputs. */
  sentAs?: string[];
}

type StoredSessionDraft = ComposerDraft & Pick<SessionDraft, "startupInputIds" | "sentAs">;

const SESSION_DRAFTS_KEY = "omnigent.sessionDrafts";
const listeners = new Set<() => void>();
const retiredDraftIds = new Set<string>();
const droppedDraftSends = new Map<string, Set<string>>();

/** Remember the sends whose text a sent or cleared draft held before a read named their inputs. */
export function rememberDroppedDraftSends(conversationId: string, sentAs: string[] = []): void {
  if (sentAs.length === 0) return;
  const dropped = droppedDraftSends.get(conversationId) ?? new Set<string>();
  for (const id of sentAs) dropped.add(id);
  droppedDraftSends.set(conversationId, dropped);
}

/** Stable ids of the sends whose text a draft held when this tab sent or cleared it, for its reads. */
export function getDroppedDraftSends(conversationId: string): string[] {
  return [...(droppedDraftSends.get(conversationId) ?? [])];
}

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
      const startupInputIds = readStoredIds(entry, "startupInputIds");
      const sentAs = readStoredIds(entry, "sentAs");
      if (draft?.text)
        drafts.set(id, {
          ...draft,
          files: [],
          ...(startupInputIds ? { startupInputIds } : {}),
          ...(sentAs ? { sentAs } : {}),
        });
    }
    return drafts;
  } catch {
    return new Map();
  }
}

function saveDraftsToStorage(): void {
  if (typeof window === "undefined") return;
  try {
    const entries: Record<string, string | StoredSessionDraft> = {};
    for (const [id, { text, replyDraft, startupInputIds, sentAs }] of sessionDrafts) {
      if (text)
        entries[id] =
          replyDraft || startupInputIds || sentAs
            ? {
                text,
                ...(replyDraft ? { replyDraft } : {}),
                ...(startupInputIds ? { startupInputIds } : {}),
                ...(sentAs ? { sentAs } : {}),
              }
            : text;
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
    rememberDroppedDraftSends(conversationId, sessionDrafts.get(conversationId)?.sentAs);
    sessionDrafts.delete(conversationId);
  } else {
    const held = sessionDrafts.get(conversationId);
    const { startupInputIds = held?.startupInputIds, sentAs = held?.sentAs, ...rest } = draft;
    // Omitted lists keep the held ids; an explicit [] clears them, and empty lists aren't stored.
    sessionDrafts.set(conversationId, {
      ...rest,
      ...(startupInputIds?.length ? { startupInputIds } : {}),
      ...(sentAs?.length ? { sentAs } : {}),
    });
  }
  saveDraftsToStorage();
  notifyListeners();
}

/**
 * Record failed startup inputs the composer is taking into a session's draft.
 *
 * @param conversationId - The session the inputs belong to.
 * @param text - Their text, saved only when the session has no draft yet; an existing draft keeps its text.
 * @param ids - Their pending ids.
 * @param files - Their files, saved with the text when the session has no draft yet.
 * @param sentAs - Stable ids of the sends the text came from, which a later read may list.
 */
export function addSessionDraftStartupInputs(
  conversationId: string,
  text: string,
  ids: string[],
  files: File[] = [],
  sentAs: string[] = [],
): void {
  const draft = sessionDrafts.get(conversationId) ?? { text, files };
  const startupInputIds = [...new Set([...(draft.startupInputIds ?? []), ...ids])];
  const sends = [...new Set([...(draft.sentAs ?? []), ...sentAs])];
  setSessionDraft(conversationId, {
    ...draft,
    ...(startupInputIds.length > 0 ? { startupInputIds } : {}),
    ...(sends.length > 0 ? { sentAs: sends } : {}),
  });
}

/**
 * Record the failed startup inputs a draft holds, in place of the inputs and sends it named: those a
 * recalled message brings back, or those a read listed under the draft's sends.
 *
 * @param conversationId - The session the inputs belong to.
 * @param text - Their text, saved only when the session has no draft yet; an existing draft keeps its text.
 * @param ids - Their pending ids, which replace the draft's.
 * @param files - Their files, saved with the text when the session has no draft yet.
 * @param sentAs - Stable ids of the draft's sends that no read has listed yet, which it keeps.
 */
export function replaceSessionDraftStartupInputs(
  conversationId: string,
  text: string,
  ids: string[],
  files: File[] = [],
  sentAs: string[] = [],
): void {
  if (retiredDraftIds.has(conversationId)) return;
  const draft = sessionDrafts.get(conversationId) ?? { text, files };
  // Without the old entry, setSessionDraft has no held ids to carry over.
  sessionDrafts.delete(conversationId);
  setSessionDraft(conversationId, {
    text: draft.text,
    files: draft.files,
    ...(draft.replyDraft ? { replyDraft: draft.replyDraft } : {}),
    ...(ids.length > 0 ? { startupInputIds: ids } : {}),
    ...(sentAs.length > 0 ? { sentAs } : {}),
  });
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

const ignoreDrafts = () => () => {};

function draftIdsKey(): string {
  return [...sessionDrafts.keys()].filter(hasSessionDraft).sort().join("\n");
}

/**
 * The ids holding a draft, as one string that changes only when a draft appears
 * or clears — so typing doesn't re-render a subscriber. Disabled: "" and no
 * subscription.
 */
export function useSessionDraftIds(enabled = true): string {
  return useSyncExternalStore(
    enabled ? subscribe : ignoreDrafts,
    enabled ? draftIdsKey : () => "",
    () => "",
  );
}

/** Clear all drafts, primarily for logout/reset flows and isolated tests. */
export function clearSessionDrafts(): void {
  sessionDrafts.clear();
  retiredDraftIds.clear();
  droppedDraftSends.clear();
  saveDraftsToStorage();
  notifyListeners();
}
