// Text-content position helpers for TipTap / ProseMirror comment anchoring.
//
// Comments are anchored by (start_index, end_index) in the raw file and by
// anchor_content (the verbatim selected text).  These helpers bridge the gap
// between raw file offsets and ProseMirror integer positions.
//
// Strategy (both directions):
//   1. Build the PM text content with "\n" between blocks as a proxy for the
//      raw file content.
//   2. Use anchor_content as the primary match key; start_index as a ±500
//      window hint so duplicate text resolves to the right occurrence.
//   3. Map between text-content offset and PM position via binary search on
//      doc.textBetween(0, mid, "\n").length — O(log n · n) for typical docs.

import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import type { Comment } from "@/hooks/useComments";

const SEP = "\n";

/**
 * Returns the smallest PM position p where
 * doc.textBetween(0, p, SEP).length >= offset.
 *
 * Binary search over PM positions — O(log(doc.size) * doc.size).
 * Adequate for typical markdown documents (< 200 KB).
 */
function textOffsetToPmPos(doc: ProseMirrorNode, offset: number): number {
  const maxSize = doc.content.size;
  if (offset <= 0) return 0;
  const total = doc.textBetween(0, maxSize, SEP).length;
  if (offset >= total) return maxSize;
  let lo = 0;
  let hi = maxSize;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (doc.textBetween(0, mid, SEP).length < offset) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * Finds the PM [from, to) range for a saved comment.
 *
 * Uses anchor_content as the text to locate; start_index (a raw-file byte
 * offset) is scaled by the textContent/rawContent ratio to produce a hint
 * for where in the text content to search first.
 *
 * Returns null when anchor_content is absent or not found in the document.
 */
export function findPmRangeForComment(
  doc: ProseMirrorNode,
  comment: Comment,
  rawContent: string,
): { from: number; to: number } | null {
  const { anchor_content, start_index } = comment;
  if (!anchor_content?.trim()) return null;

  const textContent = doc.textBetween(0, doc.content.size, SEP);
  if (!textContent) return null;

  // Scale raw offset → text-content offset as a search hint.
  const hint =
    rawContent.length > 0 ? Math.round((start_index * textContent.length) / rawContent.length) : 0;

  const WINDOW = 500;
  const searchFrom = Math.max(0, hint - WINDOW);
  let textFrom = textContent.indexOf(anchor_content, searchFrom);
  if (textFrom === -1 || textFrom > hint + WINDOW) {
    textFrom = textContent.indexOf(anchor_content); // global fallback
  }
  if (textFrom === -1) return null;

  const from = textOffsetToPmPos(doc, textFrom);
  const to = textOffsetToPmPos(doc, textFrom + anchor_content.length);
  if (from >= to) return null;

  return { from, to };
}

/**
 * Computes raw-file comment anchor data for a PM selection range.
 *
 * Extracts the selected text as anchor_content, then searches for it in
 * rawContent using the scaled text-content offset as a hint.
 *
 * When the text cannot be found verbatim in the raw file (e.g. multi-line
 * selections, table cells, or code blocks whose markdown syntax the parser
 * strips), falls back to proportionally scaled indices so the button is never
 * blocked.  The anchor_content from the PM doc is still used for re-locating
 * the highlight later via findPmRangeForComment.
 *
 * Returns null only when the selection contains no text.
 */
export function computeSelectionData(
  from: number,
  to: number,
  doc: ProseMirrorNode,
  rawContent: string,
): { start_index: number; end_index: number; anchor_content: string } | null {
  const anchor_content = doc.textBetween(from, to, SEP);
  if (!anchor_content.trim()) return null;

  const textContent = doc.textBetween(0, doc.content.size, SEP);
  const textFrom = doc.textBetween(0, from, SEP).length;

  const hint =
    textContent.length > 0 ? Math.round((textFrom * rawContent.length) / textContent.length) : 0;

  const WINDOW = 500;
  const searchFrom = Math.max(0, hint - WINDOW);
  let idx = rawContent.indexOf(anchor_content, searchFrom);
  if (idx === -1 || idx > hint + WINDOW) {
    idx = rawContent.indexOf(anchor_content);
  }

  // Fall back to proportional indices when the anchor text isn't found
  // verbatim (multi-line, table, code block selections).
  if (idx === -1) {
    return {
      start_index: hint,
      end_index: hint + anchor_content.length,
      anchor_content,
    };
  }

  return {
    start_index: idx,
    end_index: idx + anchor_content.length,
    anchor_content,
  };
}
