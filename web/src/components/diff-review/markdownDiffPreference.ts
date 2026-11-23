// Which Markdown files the reader chose to see as a rich diff, remembered per
// review (a session's changes, or one pull request) on this device. Source stays
// the default; only the most recent choices are kept.

import { useSyncExternalStore } from "react";
import { createScopedReviewMemory } from "./scopedReviewMemory";

/** How many file choices are remembered across every review. */
export const MAX_REMEMBERED_RICH_DIFFS = 200;

const memory = createScopedReviewMemory({
  storageKey: "omnigent.diffReview.richMarkdownFiles",
  changeEvent: "omnigent:rich-markdown-files-change",
  maxEntries: MAX_REMEMBERED_RICH_DIFFS,
});

const EMPTY: ReadonlySet<string> = new Set();
const pathSets = new WeakMap<ReadonlyMap<string, string>, ReadonlySet<string>>();

/** Paths shown as a rich diff in `scope`; the same set until the choice changes. */
export function readRichMarkdownPaths(scope: string): ReadonlySet<string> {
  const remembered = memory.read(scope);
  if (remembered.size === 0) return EMPTY;
  let paths = pathSets.get(remembered);
  if (!paths) {
    paths = new Set(remembered.keys());
    pathSets.set(remembered, paths);
  }
  return paths;
}

/**
 * Remember whether `path` shows as a rich diff in `scope`. The newest choice is
 * kept when the list is full; without storage, it lasts until the app reloads.
 *
 * :returns: Whether the choice was stored on this device.
 */
export function writeRichMarkdownPath(scope: string, path: string, rich: boolean): boolean {
  return memory.write(scope, path, rich ? "" : null);
}

/** Forget unsaved choices; for tests. */
export function resetUnsavedRichMarkdownPaths(): void {
  memory.resetUnsaved();
}

/** Calls `listener` whenever a choice changes, in this window or another. */
export const subscribeRichMarkdownPaths = memory.subscribe;

/** The paths shown as a rich diff in `scope`, kept live across windows. */
export function useRichMarkdownPaths(scope: string): ReadonlySet<string> {
  return useSyncExternalStore(
    subscribeRichMarkdownPaths,
    () => readRichMarkdownPaths(scope),
    () => EMPTY,
  );
}
