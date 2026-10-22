// Which hosts' harness imports this device has already reviewed, so the
// import modal opens on its own only the first time a host shows up.

import { useSyncExternalStore } from "react";

const KEY_PREFIX = "omnigent:imports-reviewed:";

export function importsReviewed(hostId: string): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem(KEY_PREFIX + hostId) !== null;
  } catch {
    // Without storage, don't nag on every load.
    return true;
  }
}

export function markImportsReviewed(hostId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(KEY_PREFIX + hostId, new Date().toISOString());
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

/** A host an explicit caller (e.g. onboarding's install step) wants reviewed. */
export interface ImportReviewTarget {
  hostId: string;
  /** Where the host runs; words the loading copy until the host is listed. */
  runner?: "remote" | "local";
}

// While set, the gate shows only this host, waiting for it to connect.
let requestedTarget: ImportReviewTarget | null = null;
const listeners = new Set<() => void>();

function setRequestedTarget(target: ImportReviewTarget | null): void {
  requestedTarget = target;
  for (const listener of listeners) listener();
}

/** Open the import modal for *target*, even before that host comes online. */
export function requestImportReview(target: ImportReviewTarget): void {
  setRequestedTarget({ ...target });
}

export function clearImportReviewRequest(): void {
  setRequestedTarget(null);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The requested target, or null when the gate picks hosts on its own. */
export function useImportReviewRequest(): ImportReviewTarget | null {
  return useSyncExternalStore(
    subscribe,
    () => requestedTarget,
    () => null,
  );
}
