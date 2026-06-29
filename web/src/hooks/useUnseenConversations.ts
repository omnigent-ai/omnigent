// Client-side tracking of which conversations have unseen messages.
//
// Stores { conversationId: wallClockSeconds } in localStorage.
// The value is the wall-clock time (seconds since epoch) when the
// user last had the conversation open. A conversation is "unseen"
// when its server-side updated_at exceeds the stored timestamp.
// Conversations with no stored entry are treated as seen (no
// baseline) so first-deploy doesn't light up every row.

import { useEffect } from "react";

const STORAGE_KEY = "omnigent:last-seen-timestamps";

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
  const baseline = atSeconds ?? nowSeconds();
  const map = readLastSeenMap();
  const stored = map[conversationId];
  if (stored !== undefined && stored >= baseline) return;
  map[conversationId] = baseline;
  writeLastSeenMap(map);
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
