// Which of several mounted boxes the person worked in last, so only that one offers its ⌘K rows
// and two open editors or comment boxes don't compete.

import { useCallback, useEffect, useId, useSyncExternalStore } from "react";

export interface LastFocused {
  /** This box was focused last. */
  active: boolean;
  /** No box in the scope holds the focus claim. */
  none: boolean;
  /** The handler its frame calls on focus. */
  markFocused: () => void;
  /** Give up the claim, as a box does once it folds. */
  release: () => void;
}

export interface LastFocusedScope {
  useLastFocused: () => LastFocused;
}

export function createLastFocusedScope(): LastFocusedScope {
  let lastFocused: string | null = null;
  const listeners = new Set<() => void>();
  const setLastFocused = (id: string | null) => {
    if (lastFocused === id) return;
    lastFocused = id;
    listeners.forEach((listener) => listener());
  };
  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  };
  return {
    useLastFocused() {
      const id = useId();
      const active = useSyncExternalStore(
        subscribe,
        () => lastFocused === id,
        () => false,
      );
      const none = useSyncExternalStore(
        subscribe,
        () => lastFocused === null,
        () => true,
      );
      const release = useCallback(() => {
        if (lastFocused === id) setLastFocused(null);
      }, [id]);
      // A box that unmounts stops being the one focused last.
      useEffect(() => release, [release]);
      const markFocused = useCallback(() => setLastFocused(id), [id]);
      return { active, none, markFocused, release };
    },
  };
}
