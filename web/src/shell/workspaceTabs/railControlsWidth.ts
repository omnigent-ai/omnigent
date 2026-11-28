// Each session's workspace sidebar reports the width its strip's controls need, so the header can
// tell whether the open sidebar shows its own Collapse button.

import { useSyncExternalStore } from "react";

const widths = new Map<string, number>();
const listeners = new Set<() => void>();

/** Record the width `sessionId`'s sidebar needs to show its controls, or forget it with null. */
export function publishRailControlsWidth(sessionId: string, width: number | null): void {
  if ((widths.get(sessionId) ?? null) === width) return;
  if (width === null) widths.delete(sessionId);
  else widths.set(sessionId, width);
  for (const listener of listeners) listener();
}

/** The width `sessionId`'s sidebar needs to show its controls, or null before it has laid out. */
export function useRailControlsWidth(sessionId: string | null | undefined): number | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => (sessionId ? (widths.get(sessionId) ?? null) : null),
  );
}
