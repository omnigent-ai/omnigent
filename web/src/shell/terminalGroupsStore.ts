// Each session's sidebar terminal groups, under their own key so an older app rewriting the whole
// workspace state can't erase them. A write touches only its own session.

import { useCallback, useSyncExternalStore } from "react";

import { parseTerminalGroups, type TerminalGroup } from "@/lib/terminalGroups";
import { withSavedSessionEntry } from "@/lib/savedSessionEntries";

export const TERMINAL_GROUPS_KEY = "omnigent:terminal-groups";

const EMPTY: readonly TerminalGroup[] = [];
const cache = new Map<string, readonly TerminalGroup[]>();

/**
 * Groups another tab's changes have left alone since they were cached, per session whose
 * cache those changes dropped, so rereading keeps them the same objects.
 */
const reusable = new Map<string, readonly TerminalGroup[]>();
const sameGroup = (first: TerminalGroup, second: TerminalGroup) =>
  first.id === second.id && JSON.stringify(first) === JSON.stringify(second);
const listeners = new Set<() => void>();

type Stored = Record<string, unknown> & { sessions?: Record<string, unknown> };

function readStored(): Stored {
  if (typeof window === "undefined") return {};
  try {
    return parseStored(window.localStorage.getItem(TERMINAL_GROUPS_KEY));
  } catch {
    return {};
  }
}

function parseStored(raw: string | null): Stored {
  try {
    const parsed: unknown = JSON.parse(raw ?? "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Stored) : {};
  } catch {
    return {};
  }
}

const sessionsOf = (stored: Stored): Record<string, unknown> =>
  stored.sessions && typeof stored.sessions === "object" && !Array.isArray(stored.sessions)
    ? stored.sessions
    : {};

export function readTerminalGroups(sessionId: string): readonly TerminalGroup[] {
  let groups = cache.get(sessionId);
  if (groups === undefined) {
    const parsed = parseTerminalGroups(sessionsOf(readStored())[sessionId]);
    const kept = reusable.get(sessionId) ?? EMPTY;
    reusable.delete(sessionId);
    groups =
      parsed.length > 0
        ? parsed.map((group) => kept.find((earlier) => sameGroup(earlier, group)) ?? group)
        : EMPTY;
    cache.set(sessionId, groups);
  }
  return groups;
}

/** Apply ``update`` to one session's groups and save them; the same array back changes nothing. */
export function updateTerminalGroups(
  sessionId: string,
  update: (current: readonly TerminalGroup[]) => readonly TerminalGroup[],
): void {
  const current = readTerminalGroups(sessionId);
  const next = update(current);
  if (next === current) return;
  const kept = next.length > 0 ? next : EMPTY;
  cache.set(sessionId, kept);
  if (typeof window !== "undefined") {
    try {
      const stored = readStored();
      const entry = kept.length > 0 ? kept : undefined;
      const sessions = withSavedSessionEntry(sessionsOf(stored), sessionId, entry);
      window.localStorage.setItem(TERMINAL_GROUPS_KEY, JSON.stringify({ ...stored, sessions }));
    } catch {
      // Full or blocked storage: the groups still hold for this page.
    }
  }
  for (const listener of listeners) listener();
}

// Another browser tab changed the groups: read them again on next use. A group that each
// change, as it was saved, left alone stays the same object, so a view's zoom on it holds;
// storage may already hold a later change, so what a group is now is always read from it.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === null) {
      cache.clear();
      reusable.clear();
    } else if (event.key === TERMINAL_GROUPS_KEY) {
      const sessions = sessionsOf(parseStored(event.newValue));
      for (const sessionId of new Set([...cache.keys(), ...reusable.keys()])) {
        const saved = parseTerminalGroups(sessions[sessionId]);
        const earlier = cache.get(sessionId) ?? reusable.get(sessionId) ?? EMPTY;
        const left = earlier.filter((group) => saved.some((now) => sameGroup(group, now)));
        cache.delete(sessionId);
        reusable.set(sessionId, left);
      }
    } else {
      return;
    }
    for (const listener of listeners) listener();
  });
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useTerminalGroups(sessionId: string): readonly TerminalGroup[] {
  const getSnapshot = useCallback(() => readTerminalGroups(sessionId), [sessionId]);
  return useSyncExternalStore(subscribe, getSnapshot);
}

/** Forget cached groups, for tests that change storage directly. */
export function resetTerminalGroupsCache(): void {
  cache.clear();
  reusable.clear();
}

// Shells a split's close is ending, per session; their groups take no changes meanwhile.
const closingShells = new Map<string, Set<string>>();
const closingListeners = new Set<() => void>();
const closingChanged = () => {
  for (const listener of closingListeners) listener();
};

/**
 * Mark ``keys`` as closing until the returned release runs: no split, focus or
 * resize changes their group meanwhile, so a shell that starts late stays its
 * own tab. Null when any of them is already closing.
 */
export function beginClosingShells(
  sessionId: string,
  keys: readonly string[],
): (() => void) | null {
  const closing = closingShells.get(sessionId) ?? new Set<string>();
  if (keys.some((key) => closing.has(key))) return null;
  for (const key of keys) closing.add(key);
  closingShells.set(sessionId, closing);
  closingChanged();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    for (const key of keys) closing.delete(key);
    if (closing.size === 0) closingShells.delete(sessionId);
    closingChanged();
  };
}

/** Whether a split's close is ending ``key`` in ``sessionId`` now. */
export function isClosingShell(sessionId: string, key: string): boolean {
  return closingShells.get(sessionId)?.has(key) ?? false;
}

/** Whether a close is ending any of the shells ``keysNow`` names, re-rendering as that changes. */
export function useShellsClosing(sessionId: string, keysNow: () => readonly string[]): boolean {
  const subscribeClosing = useCallback((listener: () => void) => {
    closingListeners.add(listener);
    return () => closingListeners.delete(listener);
  }, []);
  return useSyncExternalStore(subscribeClosing, () =>
    keysNow().some((key) => isClosingShell(sessionId, key)),
  );
}
