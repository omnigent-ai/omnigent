// The folder each session's Files tree browses, surviving unmount and remount
// within a JS session (a session switch, the mobile drawer closing, the tree
// popover closing). Keyed by conversation, so a session switch lands on that
// session's own last folder, or its workspace root. Pages around the tree
// observe it, so what they offer for the browsed folder follows it.

import { useCallback, useSyncExternalStore } from "react";

const browseLocations = new Map<string, string>();
const listeners = new Set<() => void>();

/** The folder a session's Files tree browses, absolute, or null for the workspace root. */
export function rememberedBrowseLocation(conversationId: string | undefined): string | null {
  return (conversationId && browseLocations.get(conversationId)) || null;
}

/** Remembers `location` for the session; null returns it to the workspace root. */
export function rememberBrowseLocation(conversationId: string, location: string | null): void {
  if (rememberedBrowseLocation(conversationId) === location) return;
  if (location === null) browseLocations.delete(conversationId);
  else browseLocations.set(conversationId, location);
  listeners.forEach((listener) => listener());
}

/** The session's browsed folder, re-rendering when its tree moves to another. */
export function useRememberedBrowseLocation(conversationId: string | undefined): string | null {
  const subscribe = useCallback((listener: () => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return useSyncExternalStore(subscribe, () => rememberedBrowseLocation(conversationId));
}
