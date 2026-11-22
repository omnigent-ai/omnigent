// Files the reader marked viewed, remembered per review on this device. A mark
// holds a fingerprint of the changes it saw, so a file whose changes are edited
// afterwards reads as not viewed again.

import type { FileDiffMetadata } from "@pierre/diffs";
import type { DiffReviewFile } from "./diffReviewModel";
import type { DiffReviewContent } from "./reviewItems";
import { createScopedReviewMemory, useScopedReviewMemory } from "./scopedReviewMemory";

/** How many viewed marks are remembered across every review. */
export const MAX_REMEMBERED_VIEWED_FILES = 1000;

const memory = createScopedReviewMemory({
  storageKey: "omnigent.diffReview.viewedFiles",
  changeEvent: "omnigent:viewed-files-change",
  maxEntries: MAX_REMEMBERED_VIEWED_FILES,
});

/** A short, stable hash (FNV-1a) fed piece by piece, so even huge diffs hash without copying. */
class TextHash {
  private hash = 0x811c9dc5;

  add(text: string): this {
    let hash = this.hash;
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193);
    }
    this.hash = hash;
    return this;
  }

  digest(): string {
    return (this.hash >>> 0).toString(36);
  }
}

const fingerprints = new WeakMap<FileDiffMetadata, string>();

/**
 * What a diff changes: the file lines each change starts at and the lines it
 * removes and adds. Unchanged lines don't count, so loading more context keeps it.
 */
export function diffFingerprint(fileDiff: FileDiffMetadata): string {
  const cached = fingerprints.get(fileDiff);
  if (cached !== undefined) return cached;
  const hash = new TextHash().add(fileDiff.type);
  for (const hunk of fileDiff.hunks) {
    for (const content of hunk.hunkContent) {
      if (content.type !== "change") continue;
      // File line numbers, so the same edit at another copy of a repeated line differs.
      const deletionLine = hunk.deletionStart + content.deletionLineIndex - hunk.deletionLineIndex;
      const additionLine = hunk.additionStart + content.additionLineIndex - hunk.additionLineIndex;
      hash.add(`\u0002${deletionLine},${additionLine}`);
      for (let offset = 0; offset < content.deletions; offset += 1) {
        hash.add("\u0000").add(fileDiff.deletionLines[content.deletionLineIndex + offset] ?? "");
      }
      hash.add("\u0001");
      for (let offset = 0; offset < content.additions; offset += 1) {
        hash.add("\u0000").add(fileDiff.additionLines[content.additionLineIndex + offset] ?? "");
      }
    }
  }
  const fingerprint = hash.digest();
  fingerprints.set(fileDiff, fingerprint);
  return fingerprint;
}

/**
 * The revision a viewed mark on `file` records: its changes once the diff loads,
 * or the content identity the source reports when it can't show a diff. Null when
 * there is nothing concrete to mark: while loading, after a failed load, or when
 * the source can't identify the content.
 */
export function viewedFingerprint(
  file: DiffReviewFile,
  content: DiffReviewContent | undefined,
): string | null {
  if (content?.status === "ready") return `${file.change}:${diffFingerprint(content.fileDiff)}`;
  if (content?.status === "unavailable" && content.revision) {
    return `${file.change}:source:${content.revision}`;
  }
  return null;
}

/** Why `file` can't be marked viewed yet, or null when it can. */
export function viewedUnavailableReason(
  file: DiffReviewFile,
  content: DiffReviewContent | undefined,
): string | null {
  if (viewedFingerprint(file, content) !== null) return null;
  if (content?.status !== "unavailable") return "You can mark this file viewed once its diff loads";
  return "This file can't be marked viewed because its changes can't be compared";
}

/**
 * Whether a mark recording `marked` still covers the file's current revision.
 * While the file loads (`current` is null) a mark keeps it folded until it can be compared.
 */
export function viewedMarkCovers(marked: string | undefined, current: string | null): boolean {
  if (!marked) return false;
  return current === null || marked === current;
}

/** The viewed marks in review `scope`, by path, kept live across windows. */
export function useViewedMarks(scope: string): ReadonlyMap<string, string> {
  return useScopedReviewMemory(memory, scope);
}

/**
 * Mark `path` viewed in `scope` at the revision `fingerprint` names.
 *
 * :returns: Whether the mark was stored on this device.
 */
export function markViewed(scope: string, path: string, fingerprint: string): boolean {
  return memory.write(scope, path, fingerprint);
}

/** Clear `path`'s viewed mark in `scope`. */
export function clearViewed(scope: string, path: string): boolean {
  return memory.write(scope, path, null);
}

/** What marking every file in a list viewed, or clearing every mark, would change. */
export interface ViewedBatch {
  /** Files not yet viewed whose revision is known, with the fingerprint to record. */
  toMark: { path: string; fingerprint: string }[];
  /** Files currently viewed. */
  toClear: string[];
  /** Whether a file not yet viewed is still loading, so it can't be marked yet. */
  loading: boolean;
}

/** Sorts `files` for a batch mark; files that can never be compared are left out. */
export function viewedBatch(
  files: readonly DiffReviewFile[],
  viewedPaths: ReadonlySet<string>,
  contents: ReadonlyMap<string, DiffReviewContent>,
): ViewedBatch {
  const batch: ViewedBatch = { toMark: [], toClear: [], loading: false };
  for (const file of files) {
    if (viewedPaths.has(file.path)) {
      batch.toClear.push(file.path);
      continue;
    }
    const content = contents.get(file.path);
    const fingerprint = viewedFingerprint(file, content);
    if (fingerprint !== null) batch.toMark.push({ path: file.path, fingerprint });
    else if (!file.isBinary && content?.status === "loading") batch.loading = true;
  }
  return batch;
}

/** Forget marks kept only in memory; for tests. */
export function resetUnsavedViewedMarks(): void {
  memory.resetUnsaved();
}
