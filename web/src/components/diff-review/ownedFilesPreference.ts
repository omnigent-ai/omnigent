// Whether the reader chose to see only the files they own, remembered per review
// (a session's changes, or one pull request) on this device. This is the choice
// alone: the review applies it only while the reader owns a listed file.

import { useSyncExternalStore } from "react";
import { createScopedReviewMemory } from "./scopedReviewMemory";

/** How many reviews remember the choice. */
export const MAX_REMEMBERED_OWNED_FILE_CHOICES = 200;

// The memory is keyed by path; this choice is one entry per review.
const OWNED_ONLY_KEY = "owned-only";

const memory = createScopedReviewMemory({
  storageKey: "omnigent.diffReview.ownedFilesOnly",
  changeEvent: "omnigent:owned-files-only-change",
  maxEntries: MAX_REMEMBERED_OWNED_FILE_CHOICES,
});

/** Whether the reader chose to see only their files in `scope`. */
export function readOwnedFilesOnly(scope: string): boolean {
  return memory.read(scope).has(OWNED_ONLY_KEY);
}

/**
 * Remember whether `scope` shows only the reader's files. Without storage, the
 * choice lasts until the app reloads.
 *
 * :returns: Whether the choice was stored on this device.
 */
export function writeOwnedFilesOnly(scope: string, only: boolean): boolean {
  return memory.write(scope, OWNED_ONLY_KEY, only ? "" : null);
}

/** Forget unsaved choices; for tests. */
export function resetUnsavedOwnedFilesOnly(): void {
  memory.resetUnsaved();
}

/** Whether `scope` shows only the reader's files, kept live across windows. */
export function useOwnedFilesOnly(scope: string): boolean {
  return useSyncExternalStore(
    memory.subscribe,
    () => readOwnedFilesOnly(scope),
    () => false,
  );
}
