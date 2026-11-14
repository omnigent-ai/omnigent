import { useSyncExternalStore } from "react";
import { mergeTaggedDrafts, readTaggedMentions, type TaggedMentions } from "./composerMentions";
import { readComposerDraft, type ComposerDraft } from "./replyDraft";
import type { Comment } from "@/hooks/useComments";
import { isLocalReviewComment } from "./localReviewComments";

export interface SessionDraft extends ComposerDraft {
  files: File[];
  /** Files and sessions tagged with "@", so restored tokens still expand on send. */
  mentions?: TaggedMentions;
  reviewComments?: Comment[];
}

type StoredDraft =
  string | (ComposerDraft & { mentions?: TaggedMentions; reviewComments?: Comment[] });

const SESSION_DRAFTS_KEY = "omnigent.sessionDrafts";
const listeners = new Set<() => void>();
const retiredDraftIds = new Set<string>();
// How many times this tab has written each id's draft, emptied ones included.
const draftRevisions = new Map<string, number>();

function noteDraftWrite(conversationId: string): void {
  draftRevisions.set(conversationId, (draftRevisions.get(conversationId) ?? 0) + 1);
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
      const mentions =
        typeof entry === "object" && entry !== null
          ? readTaggedMentions((entry as { mentions?: unknown }).mentions)
          : undefined;
      const rawComments =
        entry && typeof entry === "object" && "reviewComments" in entry
          ? entry.reviewComments
          : undefined;
      const reviewComments = Array.isArray(rawComments)
        ? rawComments.filter(
            (comment) => isLocalReviewComment(comment) && comment.conversation_id === id,
          )
        : [];
      if (draft && (draft.text || reviewComments.length))
        drafts.set(id, {
          ...draft,
          files: [],
          ...(mentions ? { mentions } : {}),
          ...(reviewComments.length ? { reviewComments } : {}),
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
    const entries: Record<string, StoredDraft> = {};
    for (const [id, draft] of sessionDrafts) {
      if (!draft.text && !draft.reviewComments?.length) continue;
      const tagged =
        draft.mentions && (draft.mentions.files.length > 0 || draft.mentions.sessions.length > 0)
          ? draft.mentions
          : undefined;
      entries[id] =
        draft.replyDraft || tagged || draft.reviewComments?.length
          ? {
              text: draft.text,
              ...(draft.reviewComments?.length ? { reviewComments: draft.reviewComments } : {}),
              ...(draft.replyDraft ? { replyDraft: draft.replyDraft } : {}),
              ...(tagged ? { mentions: tagged } : {}),
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
  noteDraftWrite(conversationId);
  const reviewComments = draft.reviewComments ?? sessionDrafts.get(conversationId)?.reviewComments;
  if (draft.text === "" && draft.files.length === 0 && !reviewComments?.length) {
    sessionDrafts.delete(conversationId);
  } else {
    sessionDrafts.set(conversationId, { ...draft, ...(reviewComments ? { reviewComments } : {}) });
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
export function recoverFailedSessionDraft<
  T extends { message: string; files: File[]; mentions?: TaggedMentions },
>(originalDraft: T, temporaryConversationId?: string): T {
  if (temporaryConversationId === undefined) return originalDraft;
  const temporaryDraft = retireSessionDraft(temporaryConversationId);
  if (temporaryDraft === undefined) return originalDraft;
  // The two composers allocate tokens independently; reconcile them so a shared
  // token can't make one draft's tag expand to the other's target.
  const merged = mergeTaggedDrafts(originalDraft.mentions, temporaryDraft);
  const message = [originalDraft.message, merged.text]
    .filter((part) => part.trim() !== "")
    .join("\n\n");
  return {
    ...originalDraft,
    message,
    files: [...originalDraft.files, ...temporaryDraft.files],
    ...(merged.mentions ? { mentions: merged.mentions } : {}),
  };
}

/** Move an unsent draft when a temporary conversation receives its real id. */
export function promoteSessionDraft(
  temporaryConversationId: string,
  conversationId: string,
): SessionDraft | undefined {
  const draft = retireSessionDraft(temporaryConversationId);
  if (draft === undefined) return undefined;
  noteDraftWrite(conversationId);
  sessionDrafts.set(conversationId, draft);
  saveDraftsToStorage();
  notifyListeners();
  return draft;
}

/**
 * Whether this tab has written *conversationId*'s draft, even one the user
 * then emptied, as opposed to never having touched it.
 */
export function sessionDraftWasWritten(conversationId: string): boolean {
  return draftRevisions.has(conversationId);
}

/**
 * How many times this tab has written *conversationId*'s draft; an unchanged
 * count means nothing has written it since.
 */
export function sessionDraftRevision(conversationId: string): number {
  return draftRevisions.get(conversationId) ?? 0;
}

export function hasSessionDraft(conversationId: string): boolean {
  const draft = sessionDrafts.get(conversationId);
  return (
    draft !== undefined &&
    (draft.text.trim() !== "" || draft.files.length > 0 || !!draft.reviewComments?.length)
  );
}

const NO_REVIEW_COMMENTS: Comment[] = [];

export function useSessionReviewComments(conversationId: string | null): Comment[] {
  return useSyncExternalStore(
    subscribe,
    () =>
      (conversationId ? sessionDrafts.get(conversationId)?.reviewComments : undefined) ??
      NO_REVIEW_COMMENTS,
    () => NO_REVIEW_COMMENTS,
  );
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
  draftRevisions.clear();
  saveDraftsToStorage();
  notifyListeners();
}
