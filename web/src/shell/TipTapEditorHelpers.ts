// Map raw-file comment offsets to and from ProseMirror positions, and apply
// externally changed Markdown to a live editor.

import type { Editor } from "@tiptap/core";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { EditorState } from "@tiptap/pm/state";
import { Mapping, StepMap } from "@tiptap/pm/transform";
import type { MarkdownManager } from "@tiptap/markdown";
import { exactIndex, markdownSourceMap, type SourceMap } from "./markdownSourceMap";
import type { Comment } from "@/hooks/useComments";

const SEP = "\n";

function exactRange(
  starts: number[],
  start: number,
  ends: number[],
  end: number,
): [number, number] | null {
  const first = exactIndex(starts, start);
  let last = exactIndex(ends, end);
  while (last !== -1 && ends[last + 1] === end) last++;
  return first !== -1 && last >= first ? [first, last] : null;
}

/**
 * Returns the occurrence of `needle` whose start is closest to `hint`.
 *
 * A fixed forward search window is not enough for short selections: another
 * copy of the same text can sit well within that window, and `indexOf`
 * returns the first one rather than the one the user selected.
 */
function nearestOccurrence(
  haystack: string,
  needle: string,
  hint: number,
  accept: (index: number) => boolean = () => true,
): number {
  if (!needle) return -1;
  let best = -1;
  let bestDistance = Number.POSITIVE_INFINITY;
  let from = 0;
  while (from <= haystack.length) {
    const found = haystack.indexOf(needle, from);
    if (found === -1) break;
    const distance = Math.abs(found - hint);
    if (found > hint && distance >= bestDistance) break;
    if (distance < bestDistance && accept(found)) {
      best = found;
      bestDistance = distance;
    }
    from = found + 1;
  }
  return best;
}

function mappedRange(
  doc: ProseMirrorNode,
  map: SourceMap,
  start: number,
  end: number,
  anchor: string,
): { from: number; to: number } | null {
  const indexes = exactRange(map.starts, start, map.ends, end);
  if (!indexes) return null;
  const range = { from: map.positions[indexes[0]], to: map.positions[indexes[1]] + 1 };
  return doc.textBetween(range.from, range.to, SEP) === anchor ? range : null;
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
 * Uses validated source positions from the editor’s Markdown tokens when they
 * cover the comment. Otherwise, scale the raw-file offset to a rendered-text hint.
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

  const map = markdownSourceMap(doc, rawContent, manager);
  const range = map && mappedRange(doc, map, start_index, end_index, anchor_content);
  if (range) return range;
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
 * Uses validated source positions from the editor’s Markdown tokens when they
 * cover the selection. Otherwise, search rawContent near the scaled offset.
 *
 * When the text cannot be found verbatim in the raw file (e.g. multi-line
 * selections, table cells, or code blocks whose markdown syntax the parser
 * strips), falls back to proportionally scaled indices. The rendered quote
 * still relocates the highlight through findPmRangeForComment.
 *
 * Returns null for empty selections or a fallback that identifies another mapped copy.
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

  const map = markdownSourceMap(doc, rawContent, manager);
  if (map) {
    const first = lowerBound(map.positions, from);
    const end = lowerBound(map.positions, to);
    const start_index = map.starts[first];
    const end_index = map.ends[end - 1];
    const range =
      first < end ? mappedRange(doc, map, start_index, end_index, anchor_content) : null;
    if (
      range &&
      range.from >= from &&
      range.to <= to &&
      !doc.textBetween(from, range.from, SEP) &&
      !doc.textBetween(range.to, to, SEP)
    ) {
      return { start_index, end_index, anchor_content };
    }
  }
  const textContent = doc.textBetween(0, doc.content.size, SEP);
  const textFrom = doc.textBetween(0, from, SEP).length;

  const hint =
    textContent.length > 0 ? Math.round((textFrom * rawContent.length) / textContent.length) : 0;

  // Restoration trusts mapped copies, so never store one that maps elsewhere.
  const idx = nearestOccurrence(rawContent, anchor_content, hint, (index) => {
    const range =
      map && mappedRange(doc, map, index, index + anchor_content.length, anchor_content);
    return !range || (range.from === from && range.to === to);
  });

  // Fall back to proportional indices when the anchor text isn't found
  // verbatim (multi-line, table, code block selections).
  if (idx === -1) {
    const range = map && mappedRange(doc, map, hint, hint + anchor_content.length, anchor_content);
    if (range && (range.from !== from || range.to !== to)) return null;
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

function lowerBound(values: number[], value: number): number {
  let start = 0;
  let end = values.length;
  while (start < end) {
    const middle = (start + end) >>> 1;
    if (values[middle] < value) start = middle + 1;
    else end = middle;
  }
  return start;
}

const renderedIndexes = new WeakMap<ProseMirrorNode, { text: string; positions: number[] }>();

function renderedIndex(doc: ProseMirrorNode) {
  const cached = renderedIndexes.get(doc);
  if (cached) return cached;
  let text = "";
  let firstBlock = true;
  const positions: number[] = [];
  doc.descendants((node, position) => {
    const value = node.isText
      ? node.text!
      : node.isLeaf
        ? (node.type.spec.leafText?.(node) ?? "")
        : "";
    // Match textBetween's separators, which have no selectable source character.
    if (node.isBlock && ((node.isLeaf && value) || node.isTextblock)) {
      if (!firstBlock) {
        text += SEP;
        positions.push(-1);
      }
      firstBlock = false;
    }
    text += value;
    for (let index = 0; index < value.length; index++)
      positions.push(position + (node.isText ? index : 0));
  });
  const result = { text, positions };
  renderedIndexes.set(doc, result);
  return result;
}

export function remapMarkdownComment(
  doc: ProseMirrorNode,
  comment: Comment,
  rawContent: string,
  manager: MarkdownManager,
): Comment {
  const anchor = comment.anchor_content;
  if (!anchor) return comment;
  const map = markdownSourceMap(doc, rawContent, manager);
  if (!map || mappedRange(doc, map, comment.start_index, comment.end_index, anchor)) return comment;
  const { text, positions } = renderedIndex(doc);
  let best = comment;
  let distance = Infinity;
  for (let index = text.indexOf(anchor); index !== -1; index = text.indexOf(anchor, index + 1)) {
    const first = exactIndex(map.positions, positions[index]);
    const last = exactIndex(map.positions, positions[index + anchor.length - 1]);
    if (first === -1 || last < first) continue;
    const start_index = map.starts[first];
    const end_index = map.ends[last];
    const candidateDistance = Math.abs(start_index - comment.start_index);
    if (start_index > comment.start_index && candidateDistance >= distance) break;
    if (candidateDistance >= distance) continue;
    // A decoded entity can map several characters to one indivisible source span.
    const bounds = exactRange(map.starts, start_index, map.ends, end_index);
    if (!bounds || bounds[0] !== first || bounds[1] !== last) continue;
    best = { ...comment, start_index, end_index };
    distance = candidateDistance;
  }
  return best !== comment && mappedRange(doc, map, best.start_index, best.end_index, anchor)
    ? best
    : comment;
}

/**
 * Replace the editor's document with Markdown another writer saved.
 *
 * The document is exactly what `setContent` produces. The selection keeps its
 * type and maps through the one range that changed, so a caret outside it stays
 * on its text. Undo history is cleared, as Monaco's `setValue` does: an older
 * step could otherwise revert the other writer's edit, and auto-save would
 * persist that.
 *
 * :param editor: A TipTap editor with the Markdown extension.
 * :param markdown: The file's new content, e.g. ``"# Title\n\nBody\n"``.
 */
export function applyExternalMarkdown(editor: Editor, markdown: string): void {
  const { doc: previous, selection } = editor.state;
  editor.commands.setContent(markdown, { emitUpdate: false, contentType: "markdown" });
  const { doc, plugins } = editor.state;
  editor.view.updateState(EditorState.create({ doc, plugins }));
  // Restore the selection with a dispatch so the toolbar and bubbles see it.
  editor.view.dispatch(
    editor.state.tr
      .setSelection(selection.map(doc, changedRangeMapping(previous, doc)))
      .setMeta("addToHistory", false),
  );
}

/** Map positions from `previous` to `next` around the single range where they differ. */
function changedRangeMapping(previous: ProseMirrorNode, next: ProseMirrorNode): Mapping {
  const start = previous.content.findDiffStart(next.content);
  if (start === null) return new Mapping();
  let { a: endA, b: endB } = previous.content.findDiffEnd(next.content)!;
  // Repeated content can make the backward scan pass `start`; widen both ends.
  const overlap = start - Math.min(endA, endB);
  if (overlap > 0) {
    endA += overlap;
    endB += overlap;
  }
  return new Mapping([new StepMap([start, endA - start, endB - start])]);
}
