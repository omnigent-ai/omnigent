// Per-file choices a review remembers on this device, such as which Markdown
// files show as a rich diff or which files the reader marked viewed. Each entry
// names a review (a session's changes, or one pull request), a path, and an
// optional value; only the most recent entries are kept. Without storage,
// choices last until the app reloads.

import { useSyncExternalStore } from "react";

const SEPARATOR = "\u0000";
const EMPTY: ReadonlyMap<string, string> = new Map();

export interface ScopedReviewMemory {
  /** The remembered paths in `scope` and their values; the same map until a choice changes. */
  read: (scope: string) => ReadonlyMap<string, string>;
  /**
   * Remember `value` for `path` in `scope`, or forget it with `null`.
   *
   * :returns: Whether the choice was stored on this device.
   */
  write: (scope: string, path: string, value: string | null) => boolean;
  /** Calls `listener` whenever a choice changes, in this window or another. */
  subscribe: (listener: () => void) => () => void;
  /** Forget choices kept only in memory; for tests. */
  resetUnsaved: () => void;
}

/**
 * A memory kept under `storageKey`, announcing changes with `changeEvent` and
 * keeping the newest `maxEntries` choices across every review.
 */
export function createScopedReviewMemory({
  storageKey,
  changeEvent,
  maxEntries,
}: {
  storageKey: string;
  changeEvent: string;
  maxEntries: number;
}): ScopedReviewMemory {
  // Kept in memory once storage refuses a write, so choices still work this session.
  let unsavedEntries: string[] | null = null;
  let unsavedRevision = 0;
  let cachedRevision: string | undefined;
  let cachedByScope = new Map<string, ReadonlyMap<string, string>>();

  function storedRaw(): string | null {
    try {
      return window.localStorage.getItem(storageKey);
    } catch {
      return null;
    }
  }

  function readEntries(): string[] {
    if (unsavedEntries) return unsavedEntries;
    try {
      const parsed: unknown = JSON.parse(storedRaw() ?? "[]");
      return Array.isArray(parsed)
        ? parsed.filter((entry): entry is string => typeof entry === "string")
        : [];
    } catch {
      // Unreadable or corrupt storage remembers nothing.
      return [];
    }
  }

  /** An entry's path and value within `prefix`'s review, or null for another review. */
  function parseEntry(entry: string, prefix: string): [string, string] | null {
    if (!entry.startsWith(prefix)) return null;
    const rest = entry.slice(prefix.length);
    const valueAt = rest.indexOf(SEPARATOR);
    return valueAt < 0 ? [rest, ""] : [rest.slice(0, valueAt), rest.slice(valueAt + 1)];
  }

  function read(scope: string): ReadonlyMap<string, string> {
    const revision = unsavedEntries ? `unsaved:${unsavedRevision}` : `stored:${storedRaw()}`;
    if (revision !== cachedRevision) {
      cachedRevision = revision;
      cachedByScope = new Map();
    }
    let paths = cachedByScope.get(scope);
    if (!paths) {
      const prefix = scope + SEPARATOR;
      const found = new Map<string, string>();
      for (const entry of readEntries()) {
        const parsed = parseEntry(entry, prefix);
        if (parsed) found.set(parsed[0], parsed[1]);
      }
      paths = found.size > 0 ? found : EMPTY;
      cachedByScope.set(scope, paths);
    }
    return paths;
  }

  function write(scope: string, path: string, value: string | null): boolean {
    const prefix = scope + SEPARATOR;
    const entries = readEntries().filter((entry) => parseEntry(entry, prefix)?.[0] !== path);
    // An empty value is stored as the bare path, the format the first choices used.
    if (value !== null) entries.push(prefix + path + (value ? SEPARATOR + value : ""));
    const kept = entries.slice(-maxEntries);
    let stored = true;
    if (unsavedEntries) {
      stored = false;
    } else {
      try {
        window.localStorage.setItem(storageKey, JSON.stringify(kept));
      } catch {
        stored = false;
      }
    }
    if (!stored) {
      unsavedEntries = kept;
      unsavedRevision += 1;
    }
    window.dispatchEvent(new Event(changeEvent));
    return stored;
  }

  function subscribe(listener: () => void): () => void {
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === storageKey) listener();
    };
    window.addEventListener(changeEvent, listener);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(changeEvent, listener);
      window.removeEventListener("storage", onStorage);
    };
  }

  function resetUnsaved(): void {
    unsavedEntries = null;
    cachedRevision = undefined;
  }

  return { read, write, subscribe, resetUnsaved };
}

/** `memory`'s choices in `scope`, kept live across windows. */
export function useScopedReviewMemory(
  memory: ScopedReviewMemory,
  scope: string,
): ReadonlyMap<string, string> {
  return useSyncExternalStore(
    memory.subscribe,
    () => memory.read(scope),
    () => EMPTY,
  );
}
