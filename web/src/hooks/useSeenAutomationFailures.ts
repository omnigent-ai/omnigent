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
  const write = (runIds: string[]) => {
    seen = new Set(runIds.slice(-MAX_ENTRIES));
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
  };
  /** Record runs as read; returns the ones that weren't already. */
  const markManyRead = (runIds: readonly string[]): string[] => {
    const current = snapshot();
    const added = [...new Set(runIds)].filter((runId) => !current.has(runId));
    if (added.length > 0) write([...current, ...added]);
    return added;
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
      markManyRead([runId]);
    },
    markManyRead,
    /** Forget runs, undoing {@link markManyRead}, so they surface again. */
    unmarkRead(runIds: readonly string[]) {
      const current = snapshot();
      const removed = new Set(runIds.filter((runId) => current.has(runId)));
      if (removed.size > 0) write([...current].filter((runId) => !removed.has(runId)));
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
  return {
    seen,
    markRead: store.markRead,
    markManyRead: store.markManyRead,
    unmarkRead: store.unmarkRead,
  };
}
