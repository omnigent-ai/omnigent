// The Mac read path serves only the owner its page started with: one workspace user on one server
// and API base path. After any change of owner, every pull request reads through the session's
// host until the page reloads: no Mac reads, and no per-owner state saved or kept is applied.

import { getApiBasePath } from "@/lib/basePath";
import { getOmnigentServerIdentity } from "@/lib/host";
import { getCurrentUserId } from "@/lib/identity";

/** The owner as one key: server identity, API base path and workspace user, each kept apart. */
export function readPathOwner(): string {
  return JSON.stringify([getOmnigentServerIdentity(), getApiBasePath(), getCurrentUserId()]);
}

let pageOwner: string | null = null;
let moved = false;
const movedListeners = new Set<() => void>();

/**
 * Whether the owner changed since the page's first known owner, which stays so until a reload.
 * A user not known yet is no owner: a page starts before sign-in, and a refresh can blank it.
 */
export function readPathOwnerMoved(): boolean {
  if (moved) return true;
  if (getCurrentUserId() === null) return false;
  const owner = readPathOwner();
  if (pageOwner === null) {
    pageOwner = owner;
    return false;
  }
  if (owner === pageOwner) return false;
  moved = true;
  // Found while something reads: stores re-render after that read, never during it.
  queueMicrotask(() => {
    for (const listener of [...movedListeners]) listener();
  });
  return true;
}

/** Call `listener` once the owner moves, so a store's readers decide again; returns the stop. */
export function onReadPathOwnerMoved(listener: () => void): () => void {
  movedListeners.add(listener);
  return () => movedListeners.delete(listener);
}

/** Forget the page's owner, as a reload would, so a test starts clean. */
export function resetReadPathOwnerForTests(): void {
  pageOwner = null;
  moved = false;
}
