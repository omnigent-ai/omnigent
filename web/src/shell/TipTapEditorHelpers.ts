// Text-content position helpers for TipTap / ProseMirror comment anchoring.
//
// Comments are anchored by (start_index, end_index) in the raw file and by
// anchor_content (the verbatim selected text).  These helpers bridge the gap
// between raw file offsets and ProseMirror integer positions.
//
// Strategy (both directions):
//   1. Build the PM text content with "\n" between blocks as a proxy for the
//      raw file content.
//   2. Pick the copy of anchor_content whose surrounding words match the words
//      around the selection, so a short selection resolves to the copy the user
//      picked even when the same text repeats nearby.  The scaled offset only
//      breaks ties.
//   3. Map between text-content offset and PM position via binary search on
//      doc.textBetween(0, mid, "\n").length — O(log n · n) for typical docs.

import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import type { Comment } from "@/hooks/useComments";

const SEP = "\n";

/** Words compared on each side of an anchor to tell repeated copies apart. */
const CONTEXT_WORDS = 8;
/** Characters inspected on each side of an anchor when collecting those words. */
const CONTEXT_CHARS = 200;
const WORD = /[\p{L}\p{N}]+/gu;

interface ContextWords {
  before: string[];
  after: string[];
}

const NO_CONTEXT: ContextWords = { before: [], after: [] };

/**
 * Words on the same line around `[from, to)`, nearest first.
 *
 * Markdown syntax is punctuation, so the raw file and the rendered text yield
 * the same words even when the anchor itself is wrapped in `**` or backticks.
 */
function contextWords(text: string, from: number, to: number): ContextWords {
  const head = text.slice(Math.max(0, from - CONTEXT_CHARS), from);
  const headCut = head.lastIndexOf(SEP);
  const tail = text.slice(to, to + CONTEXT_CHARS);
  const tailCut = tail.indexOf(SEP);
  const before = (headCut === -1 ? head : head.slice(headCut + 1)).match(WORD) ?? [];
  const after = (tailCut === -1 ? tail : tail.slice(0, tailCut)).match(WORD) ?? [];
  return {
    before: before.reverse().slice(0, CONTEXT_WORDS),
    after: after.slice(0, CONTEXT_WORDS),
  };
}

function commonPrefix(a: string[], b: string[]): number {
  let n = 0;
  while (n < a.length && n < b.length && a[n] === b[n]) n++;
  return n;
}

/**
 * Returns the occurrence of `needle` whose surrounding words best match
 * `context`; `hint` breaks ties.  With no context this is the occurrence
 * nearest to `hint`.  Returns -1 when `needle` does not occur.
 */
function locateOccurrence(
  haystack: string,
  needle: string,
  context: ContextWords,
  hint: number,
): number {
  if (!needle) return -1;
  const first = haystack.indexOf(needle);
  if (first === -1 || haystack.indexOf(needle, first + 1) === -1) return first;

  const maxScore = context.before.length + context.after.length;
  let best = -1;
  let bestScore = -1;
  let bestDistance = Number.POSITIVE_INFINITY;
  let from = first;
  while (from <= haystack.length) {
    const found = haystack.indexOf(needle, from);
    if (found === -1) break;
    const around = contextWords(haystack, found, found + needle.length);
    const score =
      commonPrefix(context.before, around.before) + commonPrefix(context.after, around.after);
    const distance = Math.abs(found - hint);
    if (score > bestScore || (score === bestScore && distance < bestDistance)) {
      best = found;
      bestScore = score;
      bestDistance = distance;
    }
    // Copies past the hint only get farther away, so a best match that already
    // has every context word cannot be beaten.
    if (bestScore === maxScore && found > hint && distance >= bestDistance) break;
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
 * Uses anchor_content as the text to locate. The words around start_index in
 * the raw file identify which copy of a repeated anchor the comment belongs
 * to; start_index scaled by the textContent/rawContent ratio breaks ties.
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

  const hint =
    rawContent.length > 0 ? Math.round((start_index * textContent.length) / rawContent.length) : 0;

  // The surrounding raw text is only meaningful while the stored offset still
  // points at the anchor (the file may have changed since the comment was made).
  const rawEnd = start_index + anchor_content.length;
  const context =
    rawContent.slice(start_index, rawEnd) === anchor_content
      ? contextWords(rawContent, start_index, rawEnd)
      : NO_CONTEXT;

  const textFrom = locateOccurrence(textContent, anchor_content, context, hint);
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
 * rawContent using the words around the selection; the scaled text-content
 * offset breaks ties between identical copies.
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
  const textTo = textFrom + anchor_content.length;

  const hint =
    textContent.length > 0 ? Math.round((textFrom * rawContent.length) / textContent.length) : 0;

  const context =
    textContent.slice(textFrom, textTo) === anchor_content
      ? contextWords(textContent, textFrom, textTo)
      : NO_CONTEXT;

  const idx = locateOccurrence(rawContent, anchor_content, context, hint);

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
