// What an edit or delete of one pull request comment is doing, kept apart from the cards that
// show it: the text being edited, a change on its way to GitHub, one GitHub didn't confirm,
// and text kept after the comment was deleted on GitHub. Keyed by the workspace user, the PR
// and the comment, so every card showing that comment, and one mounted later, sees the same.

import { useSyncExternalStore } from "react";
import { pullRequestCommentTooLong } from "@/newPullRequestPage/hooks/usePullRequests";
import type { HostPendingScope } from "@/pullRequestReviews/hostPendingRoute";
import type { HostChangeRoute } from "@/pullRequestPage/pullRequestPageRoute";

export type CommentKind = "issue" | "review";
export type CommentChange = "edit" | "delete";

/** What Check Again needs to read the comment again as the account that changed it. */
export interface CommentCheckTarget {
  prUrl: string;
  repository: string;
  number: number;
  commentId: string;
  kind: CommentKind;
  /** The Mac account the change was sent as; Check Again reads as it, or waits for it. */
  login: string;
  /** Set where the session's host made the change, which Check Again then reads through. */
  host?: HostChangeRoute;
  /** A review comment's conversation, which Check Again through the host looks in. */
  threadId?: string;
  /** A draft's pending review, whose read through the host Check Again looks in. */
  pendingReviewId?: string;
}

/** The account and scope a private draft was read in: its text shows only while they're current. */
export interface PrivateCommentScope {
  login: string;
  /** Read through the session's host, along this route. */
  host?: HostPendingScope;
}

export interface CommentEditEntry {
  /** Which kind of comment it is, once anything was started for it. */
  kind?: CommentKind;
  /** Set for a draft in a pending review, which only that account can see. */
  privateScope?: PrivateCommentScope;
  /**
   * The text being edited, the comment's text it started from, and the Mac account it was
   * opened as: a save after this Mac switched accounts sends nothing.
   */
  editing: { text: string; expected: string; actor?: string } | null;
  /** GitHub's text, when it changed while the person was editing. */
  conflict: { current: string } | null;
  /** A change sent, waiting for GitHub's answer. */
  pending: { change: CommentChange; requestId: string } | null;
  /** A change GitHub may or may not have made; nothing more is sent until it's checked. */
  unknown: {
    change: CommentChange;
    requestId: string;
    /** For an edit: the text sent, and the text it replaced. */
    sent?: string;
    expected?: string;
    /** The person dropped the edit's text; the hold stays until Check Again settles it. */
    textDropped?: boolean;
    check: CommentCheckTarget;
  } | null;
  /** Text kept after the comment was deleted on GitHub, until the person discards it. */
  tombstone: { text: string } | null;
  /** Why the last change didn't happen, or what Check Again found. */
  message: string | null;
  /** A Check Again under way; only the latest one's answer counts. */
  checking: number | null;
  /** The last Check Again couldn't read the comment. */
  checkFailed: boolean;
}

export const EMPTY_ENTRY: CommentEditEntry = Object.freeze({
  editing: null,
  conflict: null,
  pending: null,
  unknown: null,
  tombstone: null,
  message: null,
  checking: null,
  checkFailed: false,
});

/** The entry's name: who's editing, which PR, which comment. */
export function commentEditKey(owner: string, prUrl: string, commentId: string): string {
  return [owner, prUrl, commentId].join("\n");
}

/** Whether nothing more may be sent for the comment: a change is out, or may have happened. */
export function commentChangeHeld(entry: CommentEditEntry): boolean {
  return entry.pending !== null || entry.unknown !== null;
}

/** Whether leaving the editor would lose text: it differs from the comment it started from. */
export function editDirty(entry: CommentEditEntry): boolean {
  return entry.editing !== null && entry.editing.text !== entry.editing.expected;
}

const entries = new Map<string, CommentEditEntry>();
const listeners = new Set<() => void>();
let checkSequence = 0;
// Bumped on every change, so a PR's list of entries is rebuilt only when something changed.
let version = 0;

function emit(): void {
  version += 1;
  for (const listener of listeners) listener();
}

/** The entry for `key`, or an empty one. */
export function readCommentEdit(key: string): CommentEditEntry {
  return entries.get(key) ?? EMPTY_ENTRY;
}

/** Replace `key`'s entry with `update`'s answer; an entry with nothing left is forgotten. */
export function updateCommentEdit(
  key: string,
  update: (entry: CommentEditEntry) => CommentEditEntry,
): CommentEditEntry {
  const before = readCommentEdit(key);
  const updated = update(before);
  if (updated === before) return before;
  // A private draft's text stays private through every change that keeps any of it.
  const after =
    before.privateScope && !updated.privateScope
      ? { ...updated, privateScope: before.privateScope }
      : updated;
  const empty =
    !after.editing &&
    !after.conflict &&
    !after.pending &&
    !after.unknown &&
    !after.tombstone &&
    !after.message &&
    after.checking === null;
  if (empty) entries.delete(key);
  else entries.set(key, after);
  emit();
  return after;
}

/** A new Check Again's number; an answer counts only while it's still the entry's latest. */
export function nextCommentCheck(): number {
  checkSequence += 1;
  return checkSequence;
}

/** `key`'s entry, re-read whenever any entry changes. */
export function useCommentEdit(key: string | null): CommentEditEntry {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => (key ? readCommentEdit(key) : EMPTY_ENTRY),
    () => EMPTY_ENTRY,
  );
}

/** One comment's entry, as a PR's list of them names it. */
export interface ListedCommentEdit {
  key: string;
  commentId: string;
  entry: CommentEditEntry;
}

/** The workspace user's entries for one PR. */
export function listCommentEdits(owner: string, prUrl: string): ListedCommentEdit[] {
  const prefix = `${owner}\n${prUrl}\n`;
  return [...entries]
    .filter(([key]) => key.startsWith(prefix))
    .map(([key, entry]) => ({ key, commentId: key.slice(prefix.length), entry }));
}

const listCache = new Map<string, { version: number; list: ListedCommentEdit[] }>();

/** {@link listCommentEdits}, re-read whenever any entry changes. */
export function useCommentEditsFor(owner: string, prUrl: string | null): ListedCommentEdit[] {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => {
      if (!prUrl) return NO_EDITS;
      const name = `${owner}\n${prUrl}`;
      const cached = listCache.get(name);
      if (cached?.version === version) return cached.list;
      const list = listCommentEdits(owner, prUrl);
      listCache.set(name, { version, list });
      return list;
    },
    () => NO_EDITS,
  );
}

const NO_EDITS: ListedCommentEdit[] = [];

// The transitions, as pure functions of an entry, so each is testable on its own.

/** Open the editor on `body`, as `actor`, keeping a draft already there. */
export function startEditing(
  entry: CommentEditEntry,
  body: string,
  actor?: string,
): CommentEditEntry {
  if (entry.editing || commentChangeHeld(entry)) return entry;
  return {
    ...entry,
    editing: { text: body, expected: body, ...(actor ? { actor } : {}) },
    conflict: null,
    message: null,
  };
}

export function editText(entry: CommentEditEntry, text: string): CommentEditEntry {
  if (!entry.editing || commentChangeHeld(entry)) return entry;
  return { ...entry, editing: { ...entry.editing, text } };
}

/** Close the editor and drop its text, as Cancel or Discard does; never while a change is out. */
export function stopEditing(entry: CommentEditEntry): CommentEditEntry {
  if (commentChangeHeld(entry)) return entry;
  return { ...entry, editing: null, conflict: null, message: null };
}

/** Keep mine: the next Save replaces what GitHub has now. */
export function keepMine(entry: CommentEditEntry): CommentEditEntry {
  if (!entry.editing || !entry.conflict || commentChangeHeld(entry)) return entry;
  return {
    ...entry,
    editing: { ...entry.editing, expected: entry.conflict.current },
    conflict: null,
    message: null,
  };
}

/** Load latest: start again from what GitHub has now. */
export function loadLatest(entry: CommentEditEntry): CommentEditEntry {
  if (!entry.editing || !entry.conflict || commentChangeHeld(entry)) return entry;
  const { current } = entry.conflict;
  return {
    ...entry,
    editing: { ...entry.editing, text: current, expected: current },
    conflict: null,
    message: null,
  };
}

export function changeSent(
  entry: CommentEditEntry,
  change: CommentChange,
  requestId: string,
): CommentEditEntry {
  return { ...entry, pending: { change, requestId }, message: null, checkFailed: false };
}

/** The change was made: an edit's editor closes, and nothing is held. */
export function changeMade(entry: CommentEditEntry, requestId: string): CommentEditEntry {
  if (entry.pending?.requestId !== requestId) return entry;
  return { ...EMPTY_ENTRY };
}

/** GitHub refused it before changing anything; the text stays, with why. */
export function changeRefused(
  entry: CommentEditEntry,
  requestId: string,
  message: string,
): CommentEditEntry {
  if (entry.pending?.requestId !== requestId) return entry;
  return { ...entry, pending: null, message };
}

/** GitHub's text changed since the edit began: the editor stays, offering Keep mine or Load latest. */
export function changeConflicted(
  entry: CommentEditEntry,
  requestId: string,
  current: string,
): CommentEditEntry {
  if (entry.pending?.requestId !== requestId) return entry;
  return { ...entry, pending: null, conflict: { current }, message: null };
}

/** The comment was already gone: what was being written stays, to copy. */
export function changeFoundDeleted(
  entry: CommentEditEntry,
  requestId: string,
  message: string,
): CommentEditEntry {
  if (entry.pending?.requestId !== requestId) return entry;
  const text = entry.editing && editDirty(entry) ? entry.editing.text : null;
  return {
    ...EMPTY_ENTRY,
    tombstone: text === null ? null : { text },
    message: text === null ? message : null,
  };
}

/**
 * GitHub may have made it, or the workspace user changed before it answered: held, with the
 * text, until Check Again reads the comment. Never left waiting on an answer that won't come.
 */
export function changeUncertain(
  entry: CommentEditEntry,
  requestId: string,
  details: { sent?: string; expected?: string; check: CommentCheckTarget },
): CommentEditEntry {
  if (entry.pending?.requestId !== requestId) return entry;
  return {
    ...entry,
    pending: null,
    unknown: { change: entry.pending.change, requestId, ...details },
    message: null,
    checkFailed: false,
  };
}

export function checkStarted(entry: CommentEditEntry, check: number): CommentEditEntry {
  if (!entry.unknown) return entry;
  return { ...entry, checking: check, checkFailed: false };
}

/** Check Again couldn't read the comment: still held. Only the latest check's answer counts. */
export function checkFailed(entry: CommentEditEntry, check: number): CommentEditEntry {
  if (entry.checking !== check) return entry;
  return { ...entry, checking: null, checkFailed: true };
}

/** What Check Again found settles the change, as the comment is on GitHub now. */
export type CommentCheckOutcome =
  | { settled: "edited" }
  | { settled: "deleted" }
  | { settled: "not-made"; entry: CommentEditEntry }
  | { settled: "changed-elsewhere"; entry: CommentEditEntry }
  | { settled: "gone"; entry: CommentEditEntry };

/**
 * Settle a held change from what Check Again read: the comment present with some text, or
 * absent. Null when this isn't the latest check, or nothing is held any more.
 */
export function settleChange(
  entry: CommentEditEntry,
  check: number,
  found: { state: "present"; body: string } | { state: "absent" },
): CommentCheckOutcome | null {
  const { unknown } = entry;
  if (entry.checking !== check || !unknown) return null;
  const cleared = { ...entry, unknown: null, checking: null, checkFailed: false };
  if (unknown.change === "delete") {
    if (found.state === "absent") return { settled: "deleted" };
    return {
      settled: "not-made",
      entry: { ...cleared, message: "The comment wasn't deleted." },
    };
  }
  if (found.state === "absent") {
    const text = keptText(entry);
    return {
      settled: "gone",
      entry: { ...EMPTY_ENTRY, tombstone: text === null ? null : { text } },
    };
  }
  if (found.body === unknown.sent) return { settled: "edited" };
  if (found.body === unknown.expected) {
    return { settled: "not-made", entry: { ...cleared, message: "Your edit wasn't saved." } };
  }
  return { settled: "changed-elsewhere", entry: { ...cleared, conflict: { current: found.body } } };
}

/**
 * Drop the text kept for the person: after the comment was deleted, an unsaved edit, or the
 * edit of a change GitHub didn't confirm. That last one stays held: dropping text proves
 * nothing about what GitHub did, so only Check Again settles it.
 */
export function discardLocalText(entry: CommentEditEntry): CommentEditEntry {
  if (entry.pending) return entry;
  if (entry.unknown) {
    return {
      ...entry,
      editing: null,
      conflict: null,
      tombstone: null,
      message: null,
      unknown: { ...entry.unknown, textDropped: true },
    };
  }
  return { ...EMPTY_ENTRY };
}

/**
 * Why the edit can't be saved now, or null when it can. One rule for the Save button, ⌘↵
 * and ⌘K alike, so none of them sends what another wouldn't.
 */
export function saveBlocked(entry: CommentEditEntry): string | null {
  if (!entry.editing) return "Edit the comment first";
  if (entry.pending) return "Wait for the edit to save";
  if (entry.unknown) return "Check this comment before changing it again";
  if (entry.conflict) return "Choose Keep Mine or Load Latest first";
  if (!entry.editing.text.trim()) return "Write the comment first";
  if (pullRequestCommentTooLong(entry.editing.text)) return "This comment is too long";
  return null;
}

/**
 * What to keep in reach for an entry whose comment isn't shown: a change being sent or not
 * confirmed, text kept after a delete, or an edit with unsaved text. Null when there's none.
 */
export function needsRecovery(
  entry: CommentEditEntry,
): "pending" | "unknown" | "kept" | "draft" | null {
  if (entry.pending) return "pending";
  if (entry.unknown) return "unknown";
  if (entry.tombstone) return "kept";
  if (editDirty(entry)) return "draft";
  return null;
}

/** The text an entry keeps for the person, if any. */
export function keptText(entry: CommentEditEntry): string | null {
  if (entry.tombstone) return entry.tombstone.text;
  if (entry.editing) return entry.editing.text;
  if (entry.unknown && !entry.unknown.textDropped) return entry.unknown.sent ?? null;
  return null;
}

/** Forget every entry, so a test starts clean. */
export function resetCommentEditsForTests(): void {
  entries.clear();
  listCache.clear();
  listeners.clear();
  checkSequence = 0;
}
