import { useMemo, useSyncExternalStore } from "react";

const MAX_ENTRIES = 1_000;
const stores = new Map<string, ReturnType<typeof createStore>>();

function createStore(key: string, persist: boolean) {
  let raw: string | null = null;
  let seen: ReadonlySet<string> = new Set();
  const listeners = new Set<() => void>();
  const snapshot = () => {
    if (!persist) return seen;
    try {
      const stored = localStorage.getItem(key);
      if (stored !== raw) {
        raw = stored;
        const parsed: unknown = stored ? JSON.parse(stored) : [];
        seen = new Set(
          Array.isArray(parsed)
            ? parsed.filter((id): id is string => typeof id === "string").slice(-MAX_ENTRIES)
            : [],
        );
      }
    } catch {
      // Preserve in-memory dismissals when storage is unavailable.
    }
    return seen;
  };
  return {
    snapshot,
    subscribe(listener: () => void) {
      listeners.add(listener);
      const onStorage = (event: StorageEvent) => {
        if (event.key === key || event.key === null) listener();
      };
      window.addEventListener("storage", onStorage);
      return () => {
        listeners.delete(listener);
        window.removeEventListener("storage", onStorage);
      };
    },
    markRead(runId: string) {
      seen = new Set([...snapshot(), runId].slice(-MAX_ENTRIES));
      if (persist) {
        try {
          const next = JSON.stringify([...seen]);
          localStorage.setItem(key, next);
          raw = next;
        } catch {
          // Dismissal still works for this mounted app.
        }
      }
      listeners.forEach((listener) => listener());
    },
  };
}

/** Browser-local read state, isolated by backend, mount path and viewer. */
export function useSeenAutomationFailures(scope: string, persist: boolean) {
  const store = useMemo(() => {
    const key = `omnigent:seen-automation-failures:v1:${scope}`;
    let existing = stores.get(key);
    if (!existing) {
      existing = createStore(key, persist);
      stores.set(key, existing);
    }
    return existing;
  }, [scope, persist]);
  const seen = useSyncExternalStore(store.subscribe, store.snapshot, store.snapshot);
  return { seen, markRead: store.markRead };
}
