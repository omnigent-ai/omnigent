// The Mac read path serves only the server and API base path its page started with. After either
// changes, every pull request reads through the session's host until the page reloads: no Mac
// reads, and no per-server state saved or kept is applied. A user switch keeps per-user handling.

import { getApiBasePath } from "@/lib/basePath";
import { getOmnigentServerIdentity } from "@/lib/host";
import { getCurrentUserId } from "@/lib/identity";

/** The server as one key: its identity and API base path, kept apart. */
function readPathServer(): string {
  return JSON.stringify([getOmnigentServerIdentity(), getApiBasePath()]);
}

let pageServer: string | null = null;
let moved = false;
const movedListeners = new Set<() => void>();

/**
 * Whether the server changed since the page's first, which stays so until a reload. The first
 * server is taken once a user is known, so a page that reads before its host config lands can't latch.
 */
export function readPathServerMoved(): boolean {
  if (moved) return true;
  if (getCurrentUserId() === null) return false;
  const server = readPathServer();
  if (pageServer === null) {
    pageServer = server;
    return false;
  }
  if (server === pageServer) return false;
  moved = true;
  // Found while something reads: stores re-render after that read, never during it.
  queueMicrotask(() => {
    for (const listener of [...movedListeners]) listener();
  });
  return true;
}

/** Call `listener` once the server moves, so a store's readers decide again; returns the stop. */
export function onReadPathServerMoved(listener: () => void): () => void {
  movedListeners.add(listener);
  return () => movedListeners.delete(listener);
}

/** Forget the page's server, as a reload would, so a test starts clean. */
export function resetReadPathServerForTests(): void {
  pageServer = null;
  moved = false;
}
