import { useSyncExternalStore } from "react";
import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

type PanelSizeKey = Parameters<typeof readPanelSizePreference>[0];

type WidthUpdater = number | null | ((previous: number | null) => number | null);

/** External width store shared by independently mounted resize-hook consumers. */
export function createResizableWidthStore(
  initialWidth: number | null,
  persistPreference: (width: number | null) => void,
) {
  let preferredWidth = initialWidth;
  let width = initialWidth;
  // Width at drag start. A drag writes the live width on every pointermove,
  // so an abort (Escape, blur, …) rolls back to this instead of keeping the
  // dragged width on screen while the persisted preference holds the old one.
  let dragStartWidth: number | null = null;
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
    beginDrag: () => {
      dragStartWidth = width;
    },
    /** Restore the pre-drag width, optionally re-clamped to the current bounds. */
    rollbackDrag: (clamp?: (width: number) => number) => {
      set(dragStartWidth !== null && clamp ? clamp(dragStartWidth) : dragStartWidth);
    },
  };
}

/** A width store backed by one `panelSizePreferences` key, with a test reset. */
export function createPersistedWidthStore(key: PanelSizeKey) {
  const store = createResizableWidthStore(readPanelSizePreference(key), (width) =>
    writePanelSizePreference(key, width),
  );
  return { ...store, resetForTesting: () => store.reset(readPanelSizePreference(key)) };
}

/** Width step for a resize handle's ArrowLeft/ArrowRight keyboard resize. */
export const KEYBOARD_RESIZE_STEP_PX = 20;

/**
 * Signed width delta for an arrow keypress on a resize handle, or null when
 * the key is not a horizontal arrow. `widenKey` names the arrow that widens
 * the panel (its edge faces that way). Calls preventDefault on a handled key.
 */
export function arrowResizeDelta(
  e: React.KeyboardEvent,
  widenKey: "ArrowLeft" | "ArrowRight",
): number | null {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return null;
  e.preventDefault();
  return e.key === widenKey ? KEYBOARD_RESIZE_STEP_PX : -KEYBOARD_RESIZE_STEP_PX;
}

export type ResizableWidthStore = ReturnType<typeof createResizableWidthStore>;

/** Subscribe a component to a width store's live (effective) width. */
export function useResizableWidthSnapshot(store: ResizableWidthStore): number | null {
  return useSyncExternalStore(store.subscribe, store.getSnapshot, store.getServerSnapshot);
}
