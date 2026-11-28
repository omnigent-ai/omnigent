// Split panes whose shell didn't start. The pane keeps its slot, outside the terminal list, with
// what Try Again needs to start the same kind of shell there again.

import { useSyncExternalStore } from "react";

export interface PaneStartFailure {
  /** The shell to launch, e.g. "zsh". */
  terminal: string;
  /** Why it didn't start, from the failed create. */
  reason: string;
}

const failures = new Map<string, PaneStartFailure>();
const listeners = new Set<() => void>();

const slotId = (conversationId: string, paneKey: string) =>
  JSON.stringify([conversationId, paneKey]);

function changed(): void {
  for (const listener of listeners) listener();
}

/** Keep ``paneKey``'s slot in ``conversationId``'s splits, showing why its shell didn't start. */
export function recordPaneStartFailure(
  conversationId: string,
  paneKey: string,
  failure: PaneStartFailure,
): void {
  failures.set(slotId(conversationId, paneKey), failure);
  changed();
}

/** Forget the failure, as the slot retries or closes. */
export function clearPaneStartFailure(conversationId: string, paneKey: string): void {
  if (failures.delete(slotId(conversationId, paneKey))) changed();
}

export function paneStartFailure(
  conversationId: string,
  paneKey: string,
): PaneStartFailure | undefined {
  return failures.get(slotId(conversationId, paneKey));
}

/** Whether ``paneKey`` is a failed start's slot, which the terminal list never prunes. */
export function isFailedPaneStart(conversationId: string, paneKey: string): boolean {
  return failures.has(slotId(conversationId, paneKey));
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

/** ``paneKey``'s failure while it has one, re-rendering as it's recorded or cleared. */
export function usePaneStartFailure(
  conversationId: string,
  paneKey: string,
): PaneStartFailure | undefined {
  return useSyncExternalStore(subscribe, () => paneStartFailure(conversationId, paneKey));
}

export function resetPaneStartFailuresForTests(): void {
  failures.clear();
  changed();
}
