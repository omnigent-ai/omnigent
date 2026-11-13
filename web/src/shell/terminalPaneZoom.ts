// A zoomed pane fills its split's view until the split changes at all: a split, a
// close, a divider move or focus moving to another pane unzooms. Zoom is view state
// per split, kept in memory, so a reload shows the whole split again.

import { useCallback, useSyncExternalStore } from "react";

import { terminalLayoutKeys, type TerminalSplitState } from "@/lib/paneSplitLayout";

import type { SplitTarget } from "./terminalSplitTarget";

/**
 * The split as it was when its focused pane zoomed. Every change saves a new split,
 * so any other one, even a focus move and back, unzooms.
 */
const zooms = new Map<string, TerminalSplitState>();
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

/** ``target``'s zoomed pane, or null; a zoom its split has moved on from is dropped. */
export function zoomedPaneKey(target: SplitTarget): string | null {
  const zoomed = zooms.get(target.id);
  if (!zoomed) return null;
  if (target.read() === zoomed) return zoomed.focusedKey;
  // Dropped without notifying: the split's own change already re-renders its views.
  zooms.delete(target.id);
  return null;
}

/** Zoom ``target``'s focused pane; false when it isn't split. */
export function zoomFocusedPane(target: SplitTarget): boolean {
  const split = target.read();
  if (!split || terminalLayoutKeys(split.layout).length < 2) return false;
  zooms.set(target.id, split);
  notify();
  return true;
}

export function unzoomPane(target: SplitTarget): void {
  if (zooms.delete(target.id)) notify();
}

/** Zoom ``target``'s focused pane, or unzoom it; true when it's zoomed now. */
export function toggleZoomedPane(target: SplitTarget): boolean {
  if (zoomedPaneKey(target) !== null) {
    unzoomPane(target);
    return false;
  }
  return zoomFocusedPane(target);
}

/**
 * Maximize Pane, or Restore Panes, from the pane `paneKey`: it takes focus, then its split
 * toggles. The one action every entry point runs (its key, ⌘K, a view's menu).
 */
export function togglePaneZoom(
  target: SplitTarget,
  focus: { focus: (paneKey: string) => void },
  paneKey: string,
): boolean {
  const zoomedKey = zoomedPaneKey(target);
  focus.focus(paneKey);
  // A split that hands each placement to a shell may take the key's own action for it here.
  target.zoomed?.(paneKey, zoomedKey);
  return toggleZoomedPane(target);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * ``target``'s zoomed pane while its view shows the split, following zoom and the
 * split's changes. A view not showing it (hidden, or a shell alone) keeps the zoom.
 */
export function useZoomedPaneKey(
  target: SplitTarget,
  split: TerminalSplitState | undefined,
): string | null {
  const shown = split !== undefined;
  // Read on every render too, so the split changing re-checks the zoom.
  const getSnapshot = useCallback(() => (shown ? zoomedPaneKey(target) : null), [target, shown]);
  return useSyncExternalStore(subscribe, getSnapshot);
}
