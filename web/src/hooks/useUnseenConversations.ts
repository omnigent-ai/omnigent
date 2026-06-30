// Per-user tracking of which conversations have unseen messages.
//
// The "last seen" baseline (a wall-clock second per conversation) and the
// explicit "marked unread" override live on the SERVER, in-memory and
// per-user. The read path is the per-viewer `viewer_last_seen` /
// `viewer_unread` fields embedded in the `GET /v1/sessions` list; the write
// path is `PUT /v1/sessions/{id}/read-state`. This client keeps an in-memory
// mirror, seeded from the conversation list and written back on every
// mark-seen / mark-unread. That makes read-state shared across a user's
// devices while the server is up. It is NOT persisted server-side, so a
// server restart resets it (an accepted tradeoff — read-state has no
// durable source to rederive from).
//
// A conversation is "unseen" when its server-side updated_at exceeds the
// stored baseline. Conversations with no stored entry are treated as seen
// (no baseline) so a fresh load / post-restart doesn't light up every row.

import { useEffect, useRef, useSyncExternalStore } from "react";

import { authenticatedFetch } from "@/lib/identity";

// Bumped whenever the local mirror is written, so in-tab subscribers (the
// sidebar rows, the dock badge) recompute unseen state right away — a PUT's
// network round-trip is too slow for a click to feel live, and the
// conversations poll is slower still.
const subscribers = new Set<() => void>();
let writeVersion = 0;

function notifySubscribers(): void {
  writeVersion += 1;
  for (const cb of subscribers) cb();
}

type LastSeenMap = Record<string, number>;

// In-memory mirror of the server's per-user read-state, seeded from the
// conversation list and updated optimistically on each mutation before the
// PUT lands.
const lastSeenMap: LastSeenMap = {};
const explicitlyUnread = new Set<string>();

// Sessions already seeded from the list. Seeding is once-per-session: the
// first time a conversation is seen we copy its server `viewer_*` into the
// mirror, then ignore later list values so an in-flight poll can't clobber a
// local optimistic write. Cross-device changes after first load surface on a
// reload (a deliberate Phase-1 scope: live merge is a follow-up).
const seeded = new Set<string>();

// Until the first seed runs we don't know the server's baselines, so the
// automatic mark-seen (useMarkConversationSeen) must NOT write — a deep-link
// / reload into /c/{id} mounts ChatPage synchronously, before the list loads,
// and an early "seen" PUT would clobber an explicit unread the server is
// about to hand us. Explicit user actions still apply.
let hydrated = false;

export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

/**
 * Pushes the calling user's read-state for one conversation to the server.
 * Best-effort and fire-and-forget: a failed PUT leaves the optimistic local
 * mirror in place, which the next mutation or a reload reconciles. Skips the
 * call when there's no baseline to report (nothing meaningful to sync).
 */
async function syncReadState(conversationId: string): Promise<void> {
  const lastSeen = lastSeenMap[conversationId];
  if (lastSeen === undefined) return;
  try {
    await authenticatedFetch(`/v1/sessions/${encodeURIComponent(conversationId)}/read-state`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ last_seen: lastSeen, unread: explicitlyUnread.has(conversationId) }),
    });
  } catch {
    // Network/auth errors must not break the UI; local state stays.
  }
}

/** The read-state fields the seed reads off a conversation list item. */
export interface ReadStateSeed {
  id: string;
  viewer_last_seen?: number | null;
  viewer_unread?: boolean;
}

/**
 * Seeds the local mirror from the conversation list (the server's per-viewer
 * read path). Once-per-session: a conversation's server values are copied the
 * first time it appears, then ignored, so an in-flight list poll can't clobber
 * a local optimistic write. Flips {@link hydrated} on the first call (even for
 * an empty list) so the automatic mark-seen can resume. Notifies subscribers
 * when anything changed so rows recompute.
 */
export function seedReadState(conversations: readonly ReadStateSeed[]): void {
  let changed = false;
  for (const conv of conversations) {
    if (seeded.has(conv.id)) continue;
    seeded.add(conv.id);
    if (typeof conv.viewer_last_seen === "number") {
      lastSeenMap[conv.id] = conv.viewer_last_seen;
      changed = true;
    }
    if (conv.viewer_unread) {
      explicitlyUnread.add(conv.id);
      changed = true;
    }
  }
  if (!hydrated) {
    hydrated = true;
    changed = true;
  }
  if (changed) notifySubscribers();
}

/**
 * Seeds the read-state mirror from the conversation list once it loads.
 * Pass `undefined` while the list query is still loading: until a real
 * (possibly empty) list arrives we must NOT seed or flip `hydrated`, or the
 * transient empty list on a deep-link/reload would release the mark-seen
 * gate before the server's `viewer_*` read-state is known — clobbering a
 * cross-device unread (the very race the gate guards).
 */
export function useSeedReadState(conversations: readonly ReadStateSeed[] | undefined): void {
  useEffect(() => {
    if (conversations === undefined) return;
    seedReadState(conversations);
  }, [conversations]);
}

/**
 * Test-only: reset the module-level read-state mirror so it doesn't leak
 * between tests (the mirror is intentionally module-scoped, not React state).
 * Not used in production.
 */
export function __resetReadStateForTests(): void {
  for (const id of Object.keys(lastSeenMap)) delete lastSeenMap[id];
  explicitlyUnread.clear();
  seeded.clear();
  hydrated = false;
}

/**
 * Clears the explicit-unread override for a conversation, re-enabling
 * automatic mark-seen. Called when the user genuinely (re)opens a thread,
 * since opening it *is* reading it. Notifies subscribers and syncs the
 * cleared state to the server when it actually removed an override.
 */
export function clearUnreadOverride(conversationId: string): void {
  if (explicitlyUnread.delete(conversationId)) {
    notifySubscribers();
    void syncReadState(conversationId);
  }
}

/**
 * True when the user explicitly marked this conversation unread (and hasn't
 * reopened it since). Callers use this to lift the *active-row* dot
 * suppression — flagging the thread you're viewing shows the dot at once. It
 * does NOT lift the running-status suppression: a working session's dot still
 * waits for the turn to finish (see the dot condition in Sidebar's
 * ConversationRow).
 */
export function isExplicitlyUnread(conversationId: string): boolean {
  return explicitlyUnread.has(conversationId);
}

// `atSeconds` lets callers anchor the baseline to a server timestamp
// (e.g. a PATCH response's `updated_at`) instead of the client's wall
// clock — used to dismiss self-initiated `updated_at` bumps like a
// rename, which would otherwise flag the conversation unseen because
// the server's new updated_at can land slightly past the client's
// nowSeconds() under clock skew.
export function markConversationSeen(conversationId: string, atSeconds?: number): void {
  // A conversation the user explicitly marked unread stays unread until they
  // reopen it (which clears the override first). This guards every caller —
  // the automatic active-view marks and the self-action anchors
  // (rename / archive / move) alike.
  if (explicitlyUnread.has(conversationId)) return;
  // Before hydrate resolves we don't have the server's baselines; writing
  // now could clobber an explicit unread we're about to load (the reload
  // race). Explicit user actions below are exempt — they reflect intent.
  if (!hydrated) return;
  const baseline = atSeconds ?? nowSeconds();
  const stored = lastSeenMap[conversationId];
  if (stored !== undefined && stored >= baseline) return;
  lastSeenMap[conversationId] = baseline;
  notifySubscribers();
  void syncReadState(conversationId);
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
 * sticks. Both the baseline and the override are synced to the server, so
 * the flag also survives a reload and shows on the user's other devices.
 */
export function markConversationUnread(conversationId: string, updatedAt: number): void {
  explicitlyUnread.add(conversationId);
  lastSeenMap[conversationId] = updatedAt - 1;
  notifySubscribers();
  void syncReadState(conversationId);
}

/**
 * Subscribes the caller to read-state mirror writes and returns the current
 * write version, so a component re-renders (and recomputes
 * `isConversationUnseen`) the instant the user marks a row read/unread — not
 * on the next conversations poll.
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
  const stored = lastSeenMap[conversationId];
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
  // sitting on a thread must NOT clear the hydrated explicit-unread
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
