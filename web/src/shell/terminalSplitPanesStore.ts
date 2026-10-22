// Per-session terminal split state shared by the header split controls and
// MainTerminalView, persisted to the browser's session workspace state.

import { useCallback, useSyncExternalStore } from "react";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import type { TerminalSplitState } from "@/lib/terminalSplitLayout";

const cache = new Map<string, TerminalSplitState | undefined>();
const listeners = new Set<() => void>();

function readSplit(conversationId: string): TerminalSplitState | undefined {
  if (!cache.has(conversationId)) {
    cache.set(conversationId, readSessionWorkspaceState(conversationId).terminalSplit);
  }
  return cache.get(conversationId);
}

/** Apply ``update`` to a session's split; an undefined result means unsplit. */
export function updateTerminalSplit(
  conversationId: string,
  update: (current: TerminalSplitState | undefined) => TerminalSplitState | undefined,
): void {
  const current = readSplit(conversationId);
  const next = update(current);
  if (next === current) return;
  cache.set(conversationId, next);
  writeSessionWorkspaceState(conversationId, { terminalSplit: next });
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useTerminalSplitPanes(conversationId: string): TerminalSplitState | undefined {
  const getSnapshot = useCallback(() => readSplit(conversationId), [conversationId]);
  return useSyncExternalStore(subscribe, getSnapshot);
}
