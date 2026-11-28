// Review drafts kept for this window only, in its sessionStorage, so a reload of the window
// brings them back. Another window never reads or changes them, and closing the window or the
// app lets them go. Each draft is its own entry, written at once, so a reload can't come
// between a write and the next step.

import type { z } from "zod";

/**
 * What a write did: `saved`; `failed` when storage refused it, or became unreachable after this
 * window kept something there; or `unavailable` in a window that has never kept anything.
 */
export type DraftWriteOutcome = "saved" | "failed" | "unavailable";

export interface WindowDrafts<T> {
  /** Every draft kept for this window. */
  loadAll: () => { key: string; value: T }[];
  /** Keeps `value` under `key`, or removes the draft for null. */
  save: (key: string, value: T | null) => DraftWriteOutcome;
  /** Removes every draft this store kept, for tests starting afresh. */
  clearForTests: () => void;
}

interface Stored {
  v: 1;
  key: string;
  value: unknown;
}

const PREFIX = "omnigent:review-drafts:v1:";

function windowStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function createWindowDrafts<T>({
  namespace,
  schema,
  storage = windowStorage,
}: {
  /** Which store's drafts: one store's keys never meet another's. */
  namespace: string;
  schema: z.ZodType<T>;
  storage?: () => Storage | null;
}): WindowDrafts<T> {
  const prefix = `${PREFIX}${namespace}:`;
  /** Whether this window kept or found a draft: losing storage after that is a failure. */
  let keptAny = false;
  const storageKeys = (area: Storage) =>
    Array.from({ length: area.length }, (_, index) => area.key(index)).filter(
      (storageKey): storageKey is string => !!storageKey?.startsWith(prefix),
    );
  return {
    loadAll() {
      const area = storage();
      if (!area) return [];
      const found: { key: string; value: T }[] = [];
      let keys: string[];
      try {
        keys = storageKeys(area);
      } catch {
        return [];
      }
      // An entry it can't read is skipped; the others still come back.
      for (const storageKey of keys) {
        try {
          const stored = JSON.parse(area.getItem(storageKey) ?? "null") as Stored | null;
          const value = stored?.v === 1 ? schema.safeParse(stored.value) : null;
          if (stored && value?.success) {
            found.push({ key: stored.key, value: value.data });
            keptAny = true;
          }
        } catch {
          continue;
        }
      }
      return found;
    },
    save(key, value) {
      const area = storage();
      // A copy kept earlier may still be there, so this window can't act as if nothing was.
      if (!area) return keptAny ? "failed" : "unavailable";
      const storageKey = `${prefix}${encodeURIComponent(key)}`;
      try {
        if (value === null) area.removeItem(storageKey);
        else area.setItem(storageKey, JSON.stringify({ v: 1, key, value } satisfies Stored));
        keptAny ||= value !== null;
        return "saved";
      } catch {
        return "failed";
      }
    },
    clearForTests() {
      keptAny = false;
      const area = storage();
      if (area) for (const storageKey of storageKeys(area)) area.removeItem(storageKey);
    },
  };
}
