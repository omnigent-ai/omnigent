// Where the review puts the reader: lines it is asked to reveal, and the
// scroll position it can restore.

import type { FileDiffMetadata } from "@pierre/diffs";
import { pathFromReviewItemId } from "./diffReviewModel";
import { topCurrentSideRow } from "./renderedRows";

/** A line of a file's current version to bring into view. Each new object is one request. */
export interface ReviewRevealTarget {
  path: string;
  line: number;
  /** The version `line` numbers; the current one unless set, as for a comment on a deleted line. */
  side?: "additions" | "deletions";
  /** A 1-based column to bring into view sideways, for unwrapped long lines. */
  column?: number;
  /** The range's last line, for a range that should be centered as a whole. */
  endLine?: number;
  /** "center" always centers; "nearest" leaves an already visible line in place. */
  align: "center" | "nearest";
  /** While true, a refreshed diff of the file reveals the line again. */
  persistent?: () => boolean;
}

/**
 * The reader's place as content: the file at the viewport's top and, when one
 * is rendered there, its topmost current-version line. Restoring it reveals
 * that line again even if its context was collapsed in the meantime.
 */
export interface ReviewScrollAnchor {
  path: string;
  line?: number;
  /** How far the line's row, or else the file's top, sits below the viewport's top. */
  offset: number;
}

/** The first file still showing at the viewport's top, among rendered items. */
export function fileAtViewportTop(
  viewportTop: number,
  items: readonly { path: string; top: number; bottom: number }[],
): string | undefined {
  return items
    .filter((item) => item.bottom > viewportTop)
    .sort((left, right) => left.top - right.top)[0]?.path;
}

/** Whether lines `[line, endLine]` fit in a viewport of `height` at `lineHeight` each. */
export function rangeFits(line: number, endLine: number, lineHeight: number, height: number) {
  return (endLine - line + 1) * lineHeight <= height;
}

/** The reader's place among rendered items, each an id and the host its rows render in. */
export function readScrollAnchor(
  viewportTop: number,
  items: readonly { id: string; element: HTMLElement }[],
): ReviewScrollAnchor | undefined {
  const measured = items.map((item) => {
    const bounds = item.element.getBoundingClientRect();
    return { item, path: pathFromReviewItemId(item.id), top: bounds.top, bottom: bounds.bottom };
  });
  const path = fileAtViewportTop(viewportTop, measured);
  const top = measured.find((candidate) => candidate.path === path);
  if (!path || !top) return undefined;
  const root = top.item.element.shadowRoot;
  const row = root ? topCurrentSideRow(root, viewportTop) : undefined;
  if (row && row.top < top.bottom) return { path, line: row.line, offset: row.top - viewportTop };
  return { path, offset: top.top - viewportTop };
}

/**
 * The current-version line that an unchanged old-version line shows as, so
 * hidden context around it can be expanded; undefined for a deleted line,
 * which every diff shows.
 */
export function currentLineForOldLine(
  fileDiff: FileDiffMetadata,
  oldLine: number,
): number | undefined {
  let offset = 0;
  for (const hunk of fileDiff.hunks) {
    // A side with no lines in the hunk names the line before it, not its first line.
    const firstDeletion = hunk.deletionCount === 0 ? hunk.deletionStart + 1 : hunk.deletionStart;
    const firstAddition = hunk.additionCount === 0 ? hunk.additionStart + 1 : hunk.additionStart;
    if (oldLine < firstDeletion) return oldLine + firstAddition - firstDeletion;
    let deletionLine = firstDeletion;
    let additionLine = firstAddition;
    for (const content of hunk.hunkContent) {
      if (content.type === "context") {
        if (oldLine < deletionLine + content.lines) return additionLine + oldLine - deletionLine;
        deletionLine += content.lines;
        additionLine += content.lines;
        continue;
      }
      if (oldLine < deletionLine + content.deletions) return undefined;
      deletionLine += content.deletions;
      additionLine += content.additions;
    }
    offset = additionLine - deletionLine;
  }
  return oldLine + offset;
}

/** Whether one side's `line` is among the lines a diff's hunks carry, such as a patch's. */
export function hunksHaveLine(
  fileDiff: FileDiffMetadata,
  line: number,
  side: "additions" | "deletions",
): boolean {
  return fileDiff.hunks.some((hunk) => {
    const start = side === "additions" ? hunk.additionStart : hunk.deletionStart;
    const count = side === "additions" ? hunk.additionCount : hunk.deletionCount;
    return count > 0 && line >= start && line < start + count;
  });
}
