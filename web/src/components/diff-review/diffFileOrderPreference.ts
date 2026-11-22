// How every diff review on this device orders its files, by path or by size. One choice for all
// of them, kept live across reviews and windows, and kept apart from the file viewer's record.

import { useCallback, useSyncExternalStore } from "react";

import type { DiffFileOrder } from "./diffReviewModel";

const STORAGE_KEY = "omnigent.diffReview.fileOrder";
const CHANGE_EVENT = "omnigent:diff-file-order-change";

// Without storage the choice lasts until the app reloads.
let unsaved: DiffFileOrder | null = null;

/** The saved order; by path when nothing, or something unknown, is stored. */
export function readDiffFileOrder(): DiffFileOrder {
  if (unsaved) return unsaved;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "size" ? "size" : "path";
  } catch {
    return "path";
  }
}

/** Save `order` for every review on this device, and tell the open ones. */
export function writeDiffFileOrder(order: DiffFileOrder): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, order);
    unsaved = null;
  } catch {
    unsaved = order;
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

/** Forget an unsaved choice; for tests. */
export function resetUnsavedDiffFileOrder(): void {
  unsaved = null;
}

function subscribe(onChange: () => void): () => void {
  // Another window's choice, or its clearing storage (a null key), wins over an unsaved one here.
  const onStorage = (event: StorageEvent) => {
    if (event.key !== STORAGE_KEY && event.key !== null) return;
    unsaved = null;
    onChange();
  };
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onStorage);
  };
}

/** The order every review shows, and the way to change it. */
export function useDiffFileOrder(): [DiffFileOrder, (order: DiffFileOrder) => void] {
  const order = useSyncExternalStore(subscribe, readDiffFileOrder, () => "path" as const);
  const choose = useCallback((next: DiffFileOrder) => writeDiffFileOrder(next), []);
  return [order, choose];
}
