// Which sessions have design requests waiting or not sent. It's on the app's first paint
// (tiny), so the chat mounts the outbox strip, and loads its chunk, only once there's one.
import { useSyncExternalStore } from "react";

let sessions: ReadonlySet<string> = new Set();
const listeners = new Set<() => void>();

/** Called by the outbox whenever its requests change. */
export function setDesignRequestSessions(next: Iterable<string>): void {
  const updated = new Set(next);
  if (updated.size === sessions.size && [...updated].every((id) => sessions.has(id))) return;
  sessions = updated;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Whether `conversationId` has design requests to show. */
export function useHasDesignRequests(conversationId: string | null): boolean {
  return useSyncExternalStore(subscribe, () =>
    conversationId === null ? false : sessions.has(conversationId),
  );
}

// A hint that a page left unsent design requests saved, so a new page loads the outbox
// (which brings them back) only when there's something to bring back.
const SAVED_HINT_KEY = "omnigent.designOutbox.saved";
/** The outbox's IndexedDB database. */
export const DESIGN_OUTBOX_DATABASE = "omnigent-design-outbox";

/** Called by the outbox's storage whenever what it holds changes. */
export function noteSavedDesignRequests(saved: boolean): void {
  try {
    localStorage.setItem(SAVED_HINT_KEY, saved ? "1" : "0");
  } catch {
    // No room for the hint: with none, the next page looks for the database itself.
    try {
      localStorage.removeItem(SAVED_HINT_KEY);
    } catch {
      // Storage blocked: no hint either way, so the next page looks for the database itself.
    }
  }
}

/** Whether a page may have left requests saved: the hint, or without one, the database. */
export async function hasSavedDesignRequests(): Promise<boolean> {
  try {
    const hint = localStorage.getItem(SAVED_HINT_KEY);
    if (hint !== null) return hint === "1";
  } catch {
    // Storage blocked: look for the database.
  }
  const idb = globalThis.indexedDB;
  if (!idb) return false;
  // Can't list them: there may be some, so the outbox loads and looks.
  if (typeof idb.databases !== "function") return true;
  try {
    const databases = await idb.databases();
    return databases.some((database) => database.name === DESIGN_OUTBOX_DATABASE);
  } catch {
    return true;
  }
}

// A failed load leaves them saved, for the design tools to bring back once they load.
if (typeof window !== "undefined") {
  void hasSavedDesignRequests().then((saved) => {
    if (saved) import("./designOutbox").catch(() => {});
  });
}
