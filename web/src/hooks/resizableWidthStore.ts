import { useSyncExternalStore } from "react";

type WidthUpdater = number | null | ((previous: number | null) => number | null);

/** External width store shared by independently mounted resize-hook consumers. */
export function createResizableWidthStore(
  initialWidth: number | null,
  persistPreference: (width: number | null) => void,
) {
  let preferredWidth = initialWidth;
  let width = initialWidth;
  const listeners = new Set<() => void>();

  const set = (next: WidthUpdater, persist = false) => {
    const value = typeof next === "function" ? next(width) : next;
    if (value === width) return;
    width = value;
    if (persist) {
      preferredWidth = value;
      persistPreference(value);
    }
    for (const listener of listeners) listener();
  };

  return {
    getPreferred: () => preferredWidth,
    getSnapshot: () => width,
    getServerSnapshot: () => null,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    set,
    persist: () => {
      preferredWidth = width;
      persistPreference(width);
    },
    reset: (value: number | null) => {
      preferredWidth = value;
      set(value);
    },
  };
}

export type ResizableWidthStore = ReturnType<typeof createResizableWidthStore>;

/** Subscribe a component to a width store's live (effective) width. */
export function useResizableWidthSnapshot(store: ResizableWidthStore): number | null {
  return useSyncExternalStore(store.subscribe, store.getSnapshot, store.getServerSnapshot);
}
