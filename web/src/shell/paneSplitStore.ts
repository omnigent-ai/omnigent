// Each session's split in the panes view, saved under its own key. The Terminal view keeps its
// own split in the session workspace state; the panes view holds panes that view can't show
// (workspace tabs, a chat pane) and finer ratios, so the two never read each other's layout.

import { useCallback, useSyncExternalStore } from "react";

import { withSavedSessionEntry } from "@/lib/savedSessionEntries";
import { parseTerminalSplitState, type TerminalSplitState } from "@/lib/paneSplitLayout";

export const PANE_SPLITS_KEY = "omnigent:pane-splits";

const cache = new Map<string, TerminalSplitState | undefined>();
const listeners = new Set<() => void>();

type Stored = Record<string, unknown> & { sessions?: Record<string, unknown> };

function parseStored(raw: string | null): Stored {
  try {
    const parsed: unknown = JSON.parse(raw ?? "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Stored) : {};
  } catch {
    return {};
  }
}

function readStored(): Stored {
  if (typeof window === "undefined") return {};
  try {
    return parseStored(window.localStorage.getItem(PANE_SPLITS_KEY));
  } catch {
    return {};
  }
}

const sessionsOf = (stored: Stored): Record<string, unknown> =>
  stored.sessions && typeof stored.sessions === "object" && !Array.isArray(stored.sessions)
    ? stored.sessions
    : {};

/** The session's saved split, or undefined while it shows one pane. */
export function readPaneSplit(conversationId: string): TerminalSplitState | undefined {
  if (!cache.has(conversationId)) {
    cache.set(conversationId, parseTerminalSplitState(sessionsOf(readStored())[conversationId]));
  }
  return cache.get(conversationId);
}

/** Apply ``update`` to a session's split and save it; an undefined result means unsplit. */
export function updatePaneSplit(
  conversationId: string,
  update: (current: TerminalSplitState | undefined) => TerminalSplitState | undefined,
): void {
  const current = readPaneSplit(conversationId);
  const next = update(current);
  if (next === current) return;
  cache.set(conversationId, next);
  if (typeof window !== "undefined") {
    try {
      const stored = readStored();
      const sessions = withSavedSessionEntry(sessionsOf(stored), conversationId, next);
      window.localStorage.setItem(PANE_SPLITS_KEY, JSON.stringify({ ...stored, sessions }));
    } catch {
      // Full or blocked storage: the split still holds for this page.
    }
  }
  for (const listener of listeners) listener();
}

// Another browser tab changed the splits: re-read the ones each change touched on next use. A
// split it left alone stays the same object, so a view's zoom on it holds.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === null) cache.clear();
    else if (event.key !== PANE_SPLITS_KEY) return;
    else {
      const sessions = sessionsOf(parseStored(event.newValue));
      for (const [conversationId, split] of cache) {
        const saved = parseTerminalSplitState(sessions[conversationId]);
        if (JSON.stringify(saved) !== JSON.stringify(split)) cache.delete(conversationId);
      }
    }
    for (const listener of listeners) listener();
  });
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function usePaneSplit(conversationId: string): TerminalSplitState | undefined {
  const getSnapshot = useCallback(() => readPaneSplit(conversationId), [conversationId]);
  return useSyncExternalStore(subscribe, getSnapshot);
}

/** Forget cached splits, for tests that change storage directly. */
export function resetPaneSplitsCache(): void {
  cache.clear();
}
