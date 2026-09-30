// Which hosts' harness imports this device has already reviewed, so the
// import modal opens on its own only the first time a host shows up.

import { useSyncExternalStore } from "react";
import { getHostIdentity } from "@/lib/nativeBridge";

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

// The host an explicit request (e.g. onboarding's install step) wants reviewed.
// While set, the gate shows only this host, waiting for it to connect.
let requestedHostId: string | null = null;
const listeners = new Set<() => void>();

function setRequestedHostId(hostId: string | null): void {
  if (requestedHostId === hostId) return;
  requestedHostId = hostId;
  for (const listener of listeners) listener();
}

/** Open the import modal for *hostId*, even before that host comes online. */
export function requestImportReview(hostId: string): void {
  setRequestedHostId(hostId);
}

/** Request a review of this machine's own host; false when it has no host id yet. */
export async function requestImportReviewForThisMachine(): Promise<boolean> {
  const hostId = (await getHostIdentity())?.hostId;
  if (!hostId) return false;
  requestImportReview(hostId);
  return true;
}

export function clearImportReviewRequest(): void {
  setRequestedHostId(null);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The requested host id, or null when the gate picks hosts on its own. */
export function useImportReviewRequest(): string | null {
  return useSyncExternalStore(
    subscribe,
    () => requestedHostId,
    () => null,
  );
}
