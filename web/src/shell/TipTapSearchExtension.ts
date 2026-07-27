// ProseMirror Plugin + TipTap Extension for find-in-file search highlights.
//
// Mirrors TipTapCommentExtension: matches are drawn as ProseMirror Decorations
// (not marks) so they never affect markdown serialisation and remap through
// editing transactions. The current match gets an extra class so the find bar
// can style/scroll it.
//
// stateRef is closed over directly in createSearchDecorationExtension() rather
// than passed through configure()/addOptions(): TipTap deep-merges options,
// which clones the ref object and breaks the shared-ref contract.

import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import type { RefObject } from "react";

export const searchDecorationKey = new PluginKey<DecorationSet>("searchDecoration");

export interface SearchDecorationState {
  /** Lowercased search term; empty string means "no highlights". */
  query: string;
  /** Index of the active match within findMatches(), clamped by the caller. */
  currentIndex: number;
}

// One run of visible text in the doc, paired with its ProseMirror position so a
// match found in the flattened string can be mapped back to a doc range.
// `separator` runs (block boundaries) carry a newline in the visible text but
// zero doc width; a match touching one is rejected so highlights never span
// blocks.
interface VisibleSegment {
  text: string;
  /** Absolute PM position where this run's text begins. */
  from: number;
  /** Offset of this run within the concatenated visible text. */
  visibleFrom: number;
  separator: boolean;
}

/**
 * Flatten the doc into one visible-text string plus segments mapping each run
 * back to PM positions. Adjacent text nodes inside a block concatenate (so a
 * match can cross a formatting boundary like `**bold**`); a newline separator
 * is inserted between blocks so a match can't span two blocks.
 */
function buildVisibleTextMap(doc: ProseMirrorNode): { text: string; segments: VisibleSegment[] } {
  const segments: VisibleSegment[] = [];
  let text = "";
  let sawBlock = false;
  doc.descendants((node, pos) => {
    if (node.isTextblock) {
      if (sawBlock) {
        segments.push({ text: "\n", from: pos, visibleFrom: text.length, separator: true });
        text += "\n";
      }
      sawBlock = true;
      return true; // descend into inline children
    }
    if (node.isText && node.text) {
      segments.push({ text: node.text, from: pos, visibleFrom: text.length, separator: false });
      text += node.text;
      return false;
    }
    return true;
  });
  return { text, segments };
}

/**
 * Lowercase without changing string length: fold each character, but keep the
 * original whenever its lowercase form has a different UTF-16 length (e.g. `İ`
 * U+0130 → `i` + combining U+0307). A plain `toLowerCase()` would desync the
 * haystack from the original-text coordinate map, shifting or invalidating the
 * mapped PM positions; preserving length keeps every offset aligned. Such
 * length-changing characters simply don't case-fold (a rare, acceptable
 * trade-off); applying the same fold to both haystack and query keeps matching
 * symmetric.
 */
function lowerPreservingLength(s: string): string {
  let out = "";
  for (const ch of s) {
    const lower = ch.toLowerCase();
    out += lower.length === ch.length ? lower : ch;
  }
  return out;
}

/**
 * Case-insensitive plain-text matches of `query` across the doc, as absolute PM
 * ranges. Matches span adjacent inline nodes within a block (e.g. across a bold
 * boundary) via the visible-text map, but never cross a block boundary.
 */
export function findMatches(doc: ProseMirrorNode, query: string): { from: number; to: number }[] {
  const q = lowerPreservingLength(query);
  if (!q) return [];
  const { text, segments } = buildVisibleTextMap(doc);
  const haystack = lowerPreservingLength(text);

  const matches: { from: number; to: number }[] = [];
  let idx = haystack.indexOf(q);
  while (idx !== -1) {
    const start = idx;
    const end = idx + q.length;
    idx = haystack.indexOf(q, end);

    // Locate the segments the match starts and ends in. A match that touches a
    // separator run spans a block boundary and is skipped.
    let touchesSeparator = false;
    let first: VisibleSegment | undefined;
    let last: VisibleSegment | undefined;
    for (const seg of segments) {
      const segEnd = seg.visibleFrom + seg.text.length;
      if (segEnd <= start || seg.visibleFrom >= end) continue; // no overlap
      if (seg.separator) {
        touchesSeparator = true;
        break;
      }
      if (!first) first = seg;
      last = seg;
    }
    if (touchesSeparator || !first || !last) continue;

    matches.push({
      from: first.from + (start - first.visibleFrom),
      to: last.from + (end - last.visibleFrom),
    });
  }
  return matches;
}

function buildDecorations(
  doc: ProseMirrorNode,
  state: SearchDecorationState | null,
): DecorationSet {
  if (!state || !state.query) return DecorationSet.empty;
  const matches = findMatches(doc, state.query);
  if (matches.length === 0) return DecorationSet.empty;
  const current = ((state.currentIndex % matches.length) + matches.length) % matches.length;
  const decos = matches.map((m, i) =>
    Decoration.inline(m.from, m.to, {
      class: i === current ? "md-search-match md-search-match-current" : "md-search-match",
    }),
  );
  return DecorationSet.create(doc, decos);
}

/**
 * Creates a TipTap Extension that overlays search-match highlights.
 *
 * After mutating stateRef.current, trigger a redraw by dispatching:
 *   editor.state.tr.setMeta(searchDecorationKey, 'rebuild')
 */
export function createSearchDecorationExtension(stateRef: RefObject<SearchDecorationState | null>) {
  return Extension.create({
    name: "searchDecoration",

    addProseMirrorPlugins() {
      return [
        new Plugin({
          key: searchDecorationKey,
          state: {
            init(_, { doc }) {
              return buildDecorations(doc, stateRef.current);
            },
            apply(tr, decorations, _, newState) {
              // Explicit rebuild (query/current-match changed).
              if (tr.getMeta(searchDecorationKey)) {
                return buildDecorations(newState.doc, stateRef.current);
              }
              // An edit while a query is active re-runs the search against the
              // new doc so match offsets stay correct; otherwise just remap.
              if (tr.docChanged && stateRef.current?.query) {
                return buildDecorations(newState.doc, stateRef.current);
              }
              return decorations.map(tr.mapping, newState.doc);
            },
          },
          props: {
            decorations(state) {
              return this.getState(state) ?? DecorationSet.empty;
            },
          },
        }),
      ];
    },
  });
}
