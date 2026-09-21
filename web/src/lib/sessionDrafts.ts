import { useSyncExternalStore } from "react";
import { readComposerDraft, type ComposerDraft } from "./replyDraft";

export interface SessionDraft extends ComposerDraft {
  files: File[];
  /**
   * The unsent record this draft is an untouched recovered copy of. The
   * composer's recovery owns such a draft: it is restored while the record is
   * unacknowledged and dropped once the transcript shows the message delivered.
   * The first edit makes it an ordinary draft again.
   */
  recoveredFrom?: string;
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
      if (!draft?.text) continue;
      const recoveredFrom = recoveredFromOf(entry);
      drafts.set(id, { ...draft, files: [], ...(recoveredFrom ? { recoveredFrom } : {}) });
    }
    return drafts;
  } catch {
    return new Map();
  }
}

function recoveredFromOf(entry: unknown): string | undefined {
  if (typeof entry !== "object" || entry === null) return undefined;
  const value = (entry as { recoveredFrom?: unknown }).recoveredFrom;
  return typeof value === "string" ? value : undefined;
}

function saveDraftsToStorage(): void {
  if (typeof window === "undefined") return;
  try {
    const entries: Record<string, string | (ComposerDraft & { recoveredFrom?: string })> = {};
    for (const [id, draft] of sessionDrafts) {
      if (!draft.text) continue;
      entries[id] =
        draft.replyDraft || draft.recoveredFrom
          ? {
              text: draft.text,
              ...(draft.replyDraft ? { replyDraft: draft.replyDraft } : {}),
              ...(draft.recoveredFrom ? { recoveredFrom: draft.recoveredFrom } : {}),
            }
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

const UNSENT_MESSAGES_KEY = "omnigent.unsentMessages";

/**
 * A message whose POST the server has not acknowledged, persisted so a reload
 * mid-send can recover it. One record per message (keyed by the send's id), so
 * overlapping sends never clobber each other, and kept apart from the editable
 * composer draft: text the user is typing meanwhile must never be overwritten.
 */
export interface UnsentMessage extends ComposerDraft {
  conversationId: string;
  /** Send identity, when the send has one, so a recovered resend dedupes server-side. */
  stableId?: string;
}

// Record ids written or recovered during this page's life. A record written
// here is tracked in memory by the store (`failedSendDraft`); one recovered
// here is already in the composer. Neither is offered again until a reload —
// and a record stays stored until its POST is acknowledged, so its identity
// survives any number of reloads.
const unsentThisPage = new Set<string>();

function loadUnsentMessages(): Record<string, UnsentMessage> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.sessionStorage.getItem(UNSENT_MESSAGES_KEY);
    if (!raw) return {};
    const entries: unknown = JSON.parse(raw);
    if (typeof entries !== "object" || entries === null || Array.isArray(entries)) return {};
    const messages: Record<string, UnsentMessage> = {};
    for (const [id, entry] of Object.entries(entries)) {
      const draft = readComposerDraft(entry);
      const { conversationId, stableId } = entry as {
        conversationId?: unknown;
        stableId?: unknown;
      };
      if (!draft?.text || typeof conversationId !== "string") continue;
      messages[id] = {
        ...draft,
        conversationId,
        ...(typeof stableId === "string" ? { stableId } : {}),
      };
    }
    return messages;
  } catch {
    return {};
  }
}

function saveUnsentMessages(messages: Record<string, UnsentMessage>): void {
  if (typeof window === "undefined") return;
  try {
    if (Object.keys(messages).length === 0) {
      window.sessionStorage.removeItem(UNSENT_MESSAGES_KEY);
    } else {
      window.sessionStorage.setItem(UNSENT_MESSAGES_KEY, JSON.stringify(messages));
    }
  } catch {
    // Storage full or unavailable — the in-memory failedSendDraft still covers this page.
  }
}

/** Persist an outgoing message under `recordId` until its POST is acknowledged. Blank text is not recorded. */
export function recordUnsentMessage(recordId: string, message: UnsentMessage): void {
  if (message.text.trim() === "") return;
  unsentThisPage.add(recordId);
  const messages = loadUnsentMessages();
  messages[recordId] = message;
  saveUnsentMessages(messages);
}

/** The server answered (accepted or denied): the record has served its purpose. */
export function clearUnsentMessage(recordId: string): void {
  const messages = loadUnsentMessages();
  if (!(recordId in messages)) return;
  saveUnsentMessages(
    Object.fromEntries(Object.entries(messages).filter(([id]) => id !== recordId)),
  );
}

/**
 * The transcript already holds items with these ids: a record keyed by one of
 * them was delivered (a plain message's record id is its `stable_id`, and the
 * server persists the item under that id), so it is acknowledged even though
 * the POST's response never reached this client.
 */
export function acknowledgeUnsentMessages(itemIds: Iterable<string>): Set<string> {
  const messages = loadUnsentMessages();
  const delivered = new Set([...itemIds].filter((id) => id in messages));
  if (delivered.size === 0) return delivered;
  saveUnsentMessages(
    Object.fromEntries(Object.entries(messages).filter(([id]) => !delivered.has(id))),
  );
  return delivered;
}

/** Whether `recordId` is still stored, i.e. its POST was never acknowledged. */
export function hasUnsentMessage(recordId: string): boolean {
  return recordId in loadUnsentMessages();
}

export interface RecoverableUnsentMessage extends UnsentMessage {
  /** Storage key — the send's `stableId` for a plain message, a private id for a slash command. */
  recordId: string;
}

/**
 * The oldest unacknowledged message for `conversationId` that this page has
 * not yet recovered, or `undefined`. Does not mark it: the caller decides
 * whether it can be shown, then calls `markUnsentRecovered`.
 */
export function peekUnsentMessage(conversationId: string): RecoverableUnsentMessage | undefined {
  for (const [recordId, message] of Object.entries(loadUnsentMessages())) {
    if (message.conversationId !== conversationId || unsentThisPage.has(recordId)) continue;
    return { ...message, recordId };
  }
  return undefined;
}

/** Recovered into the composer: not offered again before a reload. The record stays until acknowledged. */
export function markUnsentRecovered(recordId: string): void {
  unsentThisPage.add(recordId);
}

/** Clear all drafts, primarily for logout/reset flows and isolated tests. */
export function clearSessionDrafts(): void {
  sessionDrafts.clear();
  retiredDraftIds.clear();
  saveDraftsToStorage();
  unsentThisPage.clear();
  saveUnsentMessages({});
  notifyListeners();
}
