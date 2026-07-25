// Per-user tracking of which conversations have unseen messages.
//
// The "last seen" baseline (a wall-clock second per conversation) and the
// explicit "marked unread" override live in THIS BROWSER (localStorage),
// mirrored best-effort to the server. The server's copy is in-memory and
// per-replica: under replica sharding the list/`/updates` request can land
// on a pod that never saw the user's read-state PUT, so its
// `viewer_last_seen` / `viewer_unread` fields can be null even for a
// session the user has read. The local copy is therefore the durable
// source; the server seed only ever *raises* a baseline (max-merge), which
// also picks up newer reads from the user's other devices when the serving
// replica happens to have them. Cross-device unread is best-effort by
// design.
//
// A conversation is "unseen" when its server-side updated_at exceeds the
// stored baseline. A conversation with no baseline anywhere seeds to its
// updated_at at load ("read as of load") — pod-independent, so the
// automatic dot for a turn finishing after load always works, and a
// null server seed can no longer permanently disable a row's dot.

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

// The browser-durable read-state, hydrated from localStorage at module
// load, raised (never lowered) by the server seed, and updated
// optimistically on each mutation before the best-effort PUT lands.
const lastSeenMap: LastSeenMap = {};
const explicitlyUnread = new Set<string>();

// localStorage persistence. Best-effort everywhere: storage can be
// missing (SSR), full, or blocked — the in-memory mirror always works.
const STORAGE_KEY = "omnigent.readState.v1";
// Bound growth: keep only the newest baselines; older sessions fall back
// to the seed's read-as-of-load behavior.
const STORAGE_MAX_ENTRIES = 1000;

function hydrateFromStorage(): void {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
    if (!raw) return;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return;
    const lastSeen = (parsed as { lastSeen?: unknown }).lastSeen;
    if (typeof lastSeen === "object" && lastSeen !== null) {
      for (const [id, value] of Object.entries(lastSeen)) {
        if (typeof value === "number") lastSeenMap[id] = value;
      }
    }
    const unread = (parsed as { unread?: unknown }).unread;
    if (Array.isArray(unread)) {
      for (const id of unread) {
        if (typeof id === "string") explicitlyUnread.add(id);
      }
    }
  } catch {
    // Corrupt/blocked storage → start empty.
  }
}

function persistToStorage(): void {
  try {
    let entries = Object.entries(lastSeenMap);
    if (entries.length > STORAGE_MAX_ENTRIES) {
      entries = entries.sort((a, b) => b[1] - a[1]).slice(0, STORAGE_MAX_ENTRIES);
    }
    globalThis.localStorage?.setItem(
      STORAGE_KEY,
      JSON.stringify({ lastSeen: Object.fromEntries(entries), unread: [...explicitlyUnread] }),
    );
  } catch {
    // Quota/blocked storage → in-memory state still serves this tab.
  }
}

hydrateFromStorage();

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
  updated_at?: number;
}

/**
 * Seeds the local mirror from the conversation list (the server's per-viewer
 * read path). Once-per-session: a conversation is merged the first time it
 * appears, then ignored, so an in-flight list poll can't clobber a local
 * optimistic write. The merge is max(localStorage baseline, server value) —
 * last-seen is monotonic, so taking the max is always safe and picks up a
 * newer read from another device when the serving replica has it. A session
 * with no baseline on either side seeds to its `updated_at` ("read as of
 * load"): pod-independent, so a replica that can't see the user's read-state
 * can never freeze a row's dot off. Flips {@link hydrated} on the first call
 * (even for an empty list) so the automatic mark-seen can resume.
 */
export function seedReadState(conversations: readonly ReadStateSeed[]): void {
  let changed = false;
  for (const conv of conversations) {
    if (seeded.has(conv.id)) continue;
    seeded.add(conv.id);
    const local = lastSeenMap[conv.id];
    const server = typeof conv.viewer_last_seen === "number" ? conv.viewer_last_seen : undefined;
    let baseline =
      local !== undefined && server !== undefined ? Math.max(local, server) : (local ?? server);
    if (baseline === undefined && typeof conv.updated_at === "number") {
      baseline = conv.updated_at;
    }
    if (baseline !== undefined && baseline !== local) {
      lastSeenMap[conv.id] = baseline;
      changed = true;
    }
    if (conv.viewer_unread && !explicitlyUnread.has(conv.id)) {
      explicitlyUnread.add(conv.id);
      changed = true;
    }
  }
  if (!hydrated) {
    hydrated = true;
    changed = true;
  }
  if (changed) {
    persistToStorage();
    notifySubscribers();
  }
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
  try {
    globalThis.localStorage?.removeItem(STORAGE_KEY);
  } catch {
    // Storage unavailable in this test environment — nothing to clear.
  }
}

/**
 * Clears the explicit-unread override for a conversation, re-enabling
 * automatic mark-seen. Called when the user genuinely (re)opens a thread,
 * since opening it *is* reading it. Notifies subscribers and syncs the
 * cleared state to the server when it actually removed an override.
 */
export function clearUnreadOverride(conversationId: string): void {
  if (explicitlyUnread.delete(conversationId)) {
    persistToStorage();
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
  persistToStorage();
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
  persistToStorage();
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
