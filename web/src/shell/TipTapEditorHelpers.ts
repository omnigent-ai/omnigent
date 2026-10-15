// Map raw-file comment offsets to and from ProseMirror positions.

import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import type { MarkdownManager } from "@tiptap/markdown";
import { exactIndex, markdownSourceMap } from "./markdownSourceMap";
import type { Comment } from "@/hooks/useComments";

const SEP = "\n";

function exactRange(
  starts: number[],
  start: number,
  ends: number[],
  end: number,
): [number, number] | null {
  const first = exactIndex(starts, start);
  const last = exactIndex(ends, end);
  return first !== -1 && last >= first ? [first, last] : null;
}

/**
 * Returns the occurrence of `needle` whose start is closest to `hint`.
 *
 * A fixed forward search window is not enough for short selections: another
 * copy of the same text can sit well within that window, and `indexOf`
 * returns the first one rather than the one the user selected.
 */
function nearestOccurrence(haystack: string, needle: string, hint: number): number {
  if (!needle) return -1;
  let best = -1;
  let bestDistance = Number.POSITIVE_INFINITY;
  let from = 0;
  while (from <= haystack.length) {
    const found = haystack.indexOf(needle, from);
    if (found === -1) break;
    const distance = Math.abs(found - hint);
    if (distance < bestDistance) {
      best = found;
      bestDistance = distance;
    } else if (found > hint) {
      break;
    }
    from = found + 1;
  }
  return best;
}

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
 * Single words use validated source positions from the editor’s Markdown tokens.
 * Otherwise, scale the raw-file character offset to a rendered-text hint.
 *
 * Returns null when anchor_content is absent or not found in the document.
 */
export function findPmRangeForComment(
  doc: ProseMirrorNode,
  comment: Comment,
  rawContent: string,
  manager?: MarkdownManager,
): { from: number; to: number } | null {
  const { anchor_content, start_index, end_index } = comment;
  if (!anchor_content?.trim()) return null;

  if (!/\s/u.test(anchor_content) && rawContent.slice(start_index, end_index) === anchor_content) {
    const map = markdownSourceMap(doc, rawContent, manager);
    if (map) {
      const indexes = exactRange(map.starts, start_index, map.ends, end_index);
      if (indexes) {
        const [first, last] = indexes;
        const range = { from: map.positions[first], to: map.positions[last] + 1 };
        if (doc.textBetween(range.from, range.to, SEP) === anchor_content) return range;
      }
    }
  }
  const textContent = doc.textBetween(0, doc.content.size, SEP);
  if (!textContent) return null;

  // Scale raw offset → text-content offset as a search hint.
  const hint =
    rawContent.length > 0 ? Math.round((start_index * textContent.length) / rawContent.length) : 0;

  const textFrom = nearestOccurrence(textContent, anchor_content, hint);
  if (textFrom === -1) return null;

  const from = textOffsetToPmPos(doc, textFrom);
  const to = textOffsetToPmPos(doc, textFrom + anchor_content.length);
  if (from >= to) return null;

  return { from, to };
}

/**
 * Computes raw-file comment anchor data for a PM selection range.
 *
 * Single words use validated source positions from the editor’s Markdown tokens.
 * Otherwise, search rawContent using the scaled text-content offset as a hint.
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
  manager?: MarkdownManager,
): { start_index: number; end_index: number; anchor_content: string } | null {
  const anchor_content = doc.textBetween(from, to, SEP);
  if (!anchor_content.trim()) return null;

  if (!/\s/u.test(anchor_content)) {
    const map = markdownSourceMap(doc, rawContent, manager);
    if (map) {
      const indexes = exactRange(map.positions, from, map.positions, to - 1);
      if (indexes) {
        const [first, last] = indexes;
        const start_index = map.starts[first];
        const end_index = map.ends[last];
        if (rawContent.slice(start_index, end_index) === anchor_content) {
          return { start_index, end_index, anchor_content };
        }
      }
    }
  }
  const textContent = doc.textBetween(0, doc.content.size, SEP);
  const textFrom = doc.textBetween(0, from, SEP).length;

  const hint =
    textContent.length > 0 ? Math.round((textFrom * rawContent.length) / textContent.length) : 0;

  const idx = nearestOccurrence(rawContent, anchor_content, hint);

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
