// Client-side tracking of which conversations have unseen messages.
//
// Stores { conversationId: wallClockSeconds } in localStorage.
// The value is the wall-clock time (seconds since epoch) when the
// user last had the conversation open. A conversation is "unseen"
// when its server-side updated_at exceeds the stored timestamp.
// Conversations with no stored entry are treated as seen (no
// baseline) so first-deploy doesn't light up every row.

import { useEffect, useRef, useSyncExternalStore } from "react";

const STORAGE_KEY = "omnigent:last-seen-timestamps";
// Persisted alongside the timestamps so an explicit "Mark as unread"
// survives a page reload — including on the thread you were viewing,
// where the auto mark-seen would otherwise clear it on remount. Like
// the timestamps, this is per-device (localStorage); cross-device
// unread would need server-side state.
const UNREAD_KEY = "omnigent:explicit-unread-ids";

// Bumped whenever the last-seen map is written, so in-tab subscribers
// (the sidebar rows, the dock badge) can recompute unseen state right
// away — localStorage writes don't fire `storage` events in the same
// tab, and the conversations poll is too slow for a click to feel live.
const subscribers = new Set<() => void>();
let writeVersion = 0;

function notifySubscribers(): void {
  writeVersion += 1;
  for (const cb of subscribers) cb();
}

function readExplicitUnread(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(UNREAD_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((x): x is string => typeof x === "string"));
  } catch {
    return new Set();
  }
}

function writeExplicitUnread(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(UNREAD_KEY, JSON.stringify([...explicitlyUnread]));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

// Conversations the user explicitly marked unread. While an id is in
// this set, the automatic "active view is being read" mark-seen
// (mount / poll / focus / navigation-away in useMarkConversationSeen)
// is suppressed for it — otherwise marking the *current* thread unread
// would be clobbered the instant the user navigates away or the list
// polls. The flag is cleared by a genuine re-open (see
// clearUnreadOverride), which then lets the normal mark-seen run.
// Hydrated from localStorage so an explicit unread survives a reload.
const explicitlyUnread = readExplicitUnread();

/**
 * Clears the explicit-unread override for a conversation, re-enabling
 * automatic mark-seen. Called when the user genuinely (re)opens a
 * thread, since opening it *is* reading it. Persists the change and
 * notifies subscribers when it actually removed an override so the dot
 * clears immediately.
 */
export function clearUnreadOverride(conversationId: string): void {
  if (explicitlyUnread.delete(conversationId)) {
    writeExplicitUnread();
    notifySubscribers();
  }
}

/**
 * True when the user explicitly marked this conversation unread (and
 * hasn't reopened it since). Callers use this to lift the *active-row*
 * dot suppression — flagging the thread you're viewing shows the dot at
 * once. It does NOT lift the running-status suppression: a working
 * session's dot still waits for the turn to finish (see the dot
 * condition in Sidebar's ConversationRow).
 */
export function isExplicitlyUnread(conversationId: string): boolean {
  return explicitlyUnread.has(conversationId);
}

type LastSeenMap = Record<string, number>;

function readLastSeenMap(): LastSeenMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return {};
    }
    return parsed as LastSeenMap;
  } catch {
    return {};
  }
}

function writeLastSeenMap(map: LastSeenMap): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
  // Even if the persist threw, notify: an in-memory recompute is
  // harmless and keeps the UI consistent with the attempted change.
  notifySubscribers();
}

export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

// `atSeconds` lets callers anchor the baseline to a server timestamp
// (e.g. a PATCH response's `updated_at`) instead of the client's wall
// clock — used to dismiss self-initiated `updated_at` bumps like a
// rename, which would otherwise flag the conversation unseen because
// the server's new updated_at can land slightly past the client's
// nowSeconds() under clock skew.
export function markConversationSeen(conversationId: string, atSeconds?: number): void {
  // A conversation the user explicitly marked unread stays unread until
  // they reopen it (which clears the override first). This guards every
  // caller — the automatic active-view marks and the self-action anchors
  // (rename / archive / move) alike.
  if (explicitlyUnread.has(conversationId)) return;
  const baseline = atSeconds ?? nowSeconds();
  const map = readLastSeenMap();
  const stored = map[conversationId];
  if (stored !== undefined && stored >= baseline) return;
  map[conversationId] = baseline;
  writeLastSeenMap(map);
}

/**
 * Forces a conversation back to "unseen" — the inverse of
 * {@link markConversationSeen}, backing the kebab's "Mark as unread".
 * The dot's condition is `updated_at > stored`, so the baseline is
 * pinned just below the conversation's current `updated_at` (rather
 * than cleared — a missing entry reads as *seen*, not unseen). The
 * row's status still gates the dot: a "running" session won't surface
 * it until the turn finishes.
 *
 * Setting {@link explicitlyUnread} keeps the flag from being instantly
 * undone by the automatic mark-seen on the *active* thread (navigation
 * away, polls, focus) — so marking the conversation you're looking at
 * sticks. The override is persisted, so it also survives a reload; it
 * clears when you reopen the thread.
 */
export function markConversationUnread(conversationId: string, updatedAt: number): void {
  explicitlyUnread.add(conversationId);
  writeExplicitUnread();
  const map = readLastSeenMap();
  map[conversationId] = updatedAt - 1;
  writeLastSeenMap(map);
}

/**
 * Subscribes the caller to last-seen map writes and returns the
 * current write version, so a component re-renders (and recomputes
 * `isConversationUnseen`) the instant the user marks a row read/unread
 * — not on the next conversations poll.
 */
export function useUnseenTick(): number {
  return useSyncExternalStore(
    (onChange) => {
      subscribers.add(onChange);
      return () => subscribers.delete(onChange);
    },
    () => writeVersion,
    () => writeVersion,
  );
}

/**
 * A conversation is "unseen" only when (a) the agent has finished
 * a turn — status is "idle" or "failed", not "running" — and
 * (b) the conversation's updated_at exceeds the wall-clock time the
 * user last had it open. This avoids false positives from the
 * user's own message sends and in-flight processing bumps.
 */
export function isConversationUnseen(
  conversationId: string,
  updatedAt: number,
  status: string | undefined,
): boolean {
  if (status === "running" || status === undefined) return false;
  const map = readLastSeenMap();
  const stored = map[conversationId];
  if (stored === undefined) return false;
  return updatedAt > stored;
}

/** True when the app window currently has focus (SSR-safe default true). */
function windowHasFocus(): boolean {
  if (typeof document === "undefined") return true;
  return typeof document.hasFocus === "function" ? document.hasFocus() : true;
}

/**
 * Marks the active conversation as seen on mount, on every poll
 * refresh (updatedAt change keeps the stored time fresh), on the
 * window regaining focus, and on cleanup (navigation away).
 * Wall-clock time is stored so any server-side update that happened
 * while the user was viewing is captured, even if the conversations
 * poll hadn't picked it up yet.
 *
 * Every mark is gated on the window having focus: a thread open in a
 * blurred window is NOT being read, so a turn finishing there must
 * stay unseen (the dock badge counts it) until focus returns. The
 * focus listener covers the return path — refocusing while the
 * thread is open marks it seen at that moment.
 */
export function useMarkConversationSeen(
  conversationId: string | undefined,
  updatedAt: number | undefined,
): void {
  // Opening a thread is reading it, so clear any explicit-unread
  // override before the mark-seen below runs (and runs first, so
  // markConversationSeen isn't no-op'd by a stale override). Keyed on
  // the id alone: a poll bumping `updatedAt` while the thread stays
  // open must NOT re-clear an override the user just set on it.
  //
  // The very first mount is skipped: an initial page load / reload while
  // sitting on a thread must NOT clear a *persisted* explicit-unread
  // override (otherwise the dot you set silently vanishes on refresh).
  // ChatPage stays mounted across in-app /c/:id navigations, so this ref
  // only resets on a real reload — genuine reopens (the id changing while
  // mounted) still clear, matching "reopen = read".
  const isInitialMount = useRef(true);
  useEffect(() => {
    const wasInitial = isInitialMount.current;
    isInitialMount.current = false;
    if (!conversationId) return;
    if (wasInitial) return;
    clearUnreadOverride(conversationId);
  }, [conversationId]);

  useEffect(() => {
    if (!conversationId || updatedAt === undefined) return;
    const markIfFocused = () => {
      if (windowHasFocus()) markConversationSeen(conversationId);
    };
    markIfFocused();
    window.addEventListener("focus", markIfFocused);
    return () => {
      window.removeEventListener("focus", markIfFocused);
      // Navigation away normally happens via user interaction (focused);
      // an unmount in a blurred window (e.g. the session deleted from
      // another client) must not silently mark the thread read.
      markIfFocused();
    };
  }, [conversationId, updatedAt]);
}
