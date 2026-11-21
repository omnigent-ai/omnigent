// Where saved comments sit in a file's current text. Comments anchor by
// character offset; when the text moves, open comments follow their anchor.

import type { Comment } from "@/hooks/useComments";
import { detectLang } from "./codeViewerHelpers";
import { isPdfAnchor } from "./pdfCommentHelpers";

export function isFormattedMarkdownAnchor(comment: Comment): boolean {
  return (
    !!comment.anchor_content &&
    detectLang(comment.path) === "markdown" &&
    comment.end_index - comment.start_index !== comment.anchor_content.length
  );
}

/**
 * Classify comments into open/addressed and remap open draft comments to
 * their correct absolute offsets when the file content has changed.
 *
 * - **open**: draft comment — kept with updated offsets if the anchor
 *   moved, or at stored offsets if the anchor is no longer present
 *   (never silently dropped).
 * - **addressed**: handled by the agent or user (status "addressed").
 *
 * Markdown formatting/entity spans use the source map to avoid unrelated literal copies.
 */
/**
 * Where `anchor` starts in `content`, nearest `near`, or -1 when it's nowhere.
 *
 * Searches near the old offset first, for the common small edit before the anchor, and only
 * then the whole text, so a nearby edit doesn't jump to another occurrence of the same text.
 */
export function anchorStartIn(content: string, anchor: string, near: number): number {
  const SEARCH_WINDOW = 200;
  const windowStart = Math.max(0, near - SEARCH_WINDOW);
  const windowEnd = Math.min(content.length, near + anchor.length + SEARCH_WINDOW);
  let nearbyIdx = -1;
  let nearbyDistance = Number.POSITIVE_INFINITY;
  let from = content.indexOf(anchor, windowStart);
  while (from !== -1 && from <= windowEnd) {
    const distance = Math.abs(from - near);
    if (distance < nearbyDistance) {
      nearbyIdx = from;
      nearbyDistance = distance;
    }
    from = content.indexOf(anchor, from + 1);
  }
  return nearbyIdx !== -1 ? nearbyIdx : content.indexOf(anchor);
}

export function classifyAndRemapComments(
  comments: Comment[],
  fileContent: string,
  remapFormatted?: ((comment: Comment, content: string) => Comment) | null,
): { open: Comment[]; addressed: Comment[] } {
  const open: Comment[] = [];
  const addressed: Comment[] = [];

  for (const c of comments) {
    if (c.status === "addressed") {
      addressed.push(c);
      continue;
    }
    // Draft with no anchor — keep as-is.
    if (!c.anchor_content) {
      open.push(c);
      continue;
    }
    // PDF anchors store geometry in anchor_content; byte-offset remapping does
    // not apply to binary PDF content.
    if (isPdfAnchor(c.anchor_content)) {
      open.push(c);
      continue;
    }
    // File not yet loaded — keep at stored offsets rather than dropping.
    if (!fileContent) {
      open.push(c);
      continue;
    }
    // Keep raw spans while loading; a failed import (null) uses legacy search.
    if (isFormattedMarkdownAnchor(c) && remapFormatted !== null) {
      open.push(remapFormatted?.(c, fileContent) ?? c);
      continue;
    }
    const idx = anchorStartIn(fileContent, c.anchor_content, c.start_index);
    if (idx === -1) {
      // Anchor not found anywhere — keep at stored offsets rather than dropping.
      open.push(c);
      continue;
    }
    if (idx !== c.start_index) {
      // Text moved — update offsets.
      open.push({ ...c, start_index: idx, end_index: idx + c.anchor_content.length });
    } else {
      open.push(c);
    }
  }

  return { open, addressed };
}
