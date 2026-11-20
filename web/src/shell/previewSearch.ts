// Find-in-file matching for the rendered markdown / notebook preview.
//
// The preview is React-owned DOM (react-markdown output), so match highlights
// must NOT mutate the node tree — that would fight React's reconciliation.
// Instead we locate matches as DOM `Range`s and paint them with the CSS Custom
// Highlight API (the same approach htmlCommentBridge uses for the HTML preview),
// which overlays styling without touching the nodes.
//
// Matching mirrors the editor's TipTapSearchExtension: text is flattened across
// inline nodes so a term split by formatting (e.g. `<em>`) still matches, while
// a block boundary inserts a separator so a match never spans two blocks.

// Block-level tags whose boundary breaks a run of visible text. A match may
// span inline formatting within one block, but never cross into another.
const BLOCK_TAGS = new Set([
  "ADDRESS",
  "ARTICLE",
  "ASIDE",
  "BLOCKQUOTE",
  "DD",
  "DIV",
  "DL",
  "DT",
  "FIGCAPTION",
  "FIGURE",
  "FOOTER",
  "H1",
  "H2",
  "H3",
  "H4",
  "H5",
  "H6",
  "HEADER",
  "HR",
  "LI",
  "MAIN",
  "NAV",
  "OL",
  "P",
  "PRE",
  "SECTION",
  "TABLE",
  "TBODY",
  "TD",
  "TFOOT",
  "TH",
  "THEAD",
  "TR",
  "UL",
]);

/**
 * Lowercase without changing string length: fold each character, but keep the
 * original whenever its lowercase form has a different UTF-16 length (e.g. `İ`
 * U+0130 → `i` + combining U+0307). Preserving length keeps the search offsets
 * aligned with the original-text coordinate map used to build Ranges.
 */
function lowerPreservingLength(s: string): string {
  let out = "";
  for (const ch of s) {
    const lower = ch.toLowerCase();
    out += lower.length === ch.length ? lower : ch;
  }
  return out;
}

/** Nearest block-level ancestor of `node` within `root` (root itself if none). */
function nearestBlock(node: Node, root: Element): Element {
  let el = node.parentElement;
  while (el && el !== root) {
    if (BLOCK_TAGS.has(el.tagName)) return el;
    el = el.parentElement;
  }
  return root;
}

interface TextSegment {
  node: Text;
  /** Offset of this run's text within the flattened string. */
  visibleFrom: number;
  length: number;
}

/** Where one match starts and ends, as text nodes and offsets within them. */
export interface TextMatch {
  startNode: Text;
  startOffset: number;
  endNode: Text;
  endOffset: number;
}

/**
 * Case-insensitive plain-text matches of `query` within `container`, returned as
 * DOM Ranges ready to paint. Matches span adjacent text nodes inside one block
 * but never cross a block boundary. Returns [] for an empty query or when no
 * text matches.
 */
export function findTextRanges(container: Element, query: string): Range[] {
  return findTextMatches(container, query).map((match) => {
    const range = document.createRange();
    range.setStart(match.startNode, match.startOffset);
    range.setEnd(match.endNode, match.endOffset);
    return range;
  });
}

/** The matches {@link findTextRanges} returns, before they become Ranges. */
export function findTextMatches(container: Element, query: string): TextMatch[] {
  const q = lowerPreservingLength(query);
  if (!q) return [];

  const segments: TextSegment[] = [];
  const separators: number[] = []; // flattened-string indices that break blocks
  let text = "";
  let prevBlock: Element | null = null;

  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const value = node.nodeValue;
    if (!value) continue;
    const block = nearestBlock(node, container);
    if (prevBlock !== null && block !== prevBlock) {
      separators.push(text.length);
      text += "\n";
    }
    prevBlock = block;
    segments.push({ node: node as Text, visibleFrom: text.length, length: value.length });
    text += value;
  }

  const haystack = lowerPreservingLength(text);
  const matches: TextMatch[] = [];
  // Matches come in order, so the separator and segment lookups only move
  // forward: one pass over each, not a scan per match.
  let separator = 0;
  let segment = 0;
  let idx = haystack.indexOf(q);
  while (idx !== -1) {
    const start = idx;
    const end = idx + q.length;
    idx = haystack.indexOf(q, end);

    // A match touching a block separator spans two blocks — skip it.
    while (separator < separators.length && separators[separator] < start) separator += 1;
    if (separator < separators.length && separators[separator] < end) continue;

    while (
      segment < segments.length &&
      segments[segment].visibleFrom + segments[segment].length <= start
    ) {
      segment += 1;
    }
    const startSeg = segments[segment];
    if (!startSeg || startSeg.visibleFrom > start) continue;
    let last = segment;
    while (last < segments.length && segments[last].visibleFrom + segments[last].length < end) {
      last += 1;
    }
    const endSeg = segments[last];
    if (!endSeg || endSeg.visibleFrom >= end) continue;

    matches.push({
      startNode: startSeg.node,
      startOffset: start - startSeg.visibleFrom,
      endNode: endSeg.node,
      endOffset: end - endSeg.visibleFrom,
    });
  }
  return matches;
}

const HIGHLIGHT_ALL = "md-preview-search";
const HIGHLIGHT_CURRENT = "md-preview-search-current";

/** True when the browser supports the CSS Custom Highlight API. */
export function highlightsSupported(): boolean {
  return typeof CSS !== "undefined" && !!CSS.highlights && typeof Highlight !== "undefined";
}

/**
 * Paint the given ranges as the "all matches" highlight, with `ranges[current]`
 * promoted to the "current match" highlight. No-op where the API is missing.
 */
export function paintPreviewHighlights(ranges: Range[], current: number): void {
  if (!highlightsSupported()) return;
  const currentRange = ranges[current];
  // Added one by one: spreading a large table's matches into the constructor
  // can pass more arguments than the engine allows.
  const all = new Highlight();
  ranges.forEach((range, index) => {
    if (index !== current) all.add(range);
  });
  CSS.highlights.set(HIGHLIGHT_ALL, all);
  CSS.highlights.set(HIGHLIGHT_CURRENT, new Highlight(...(currentRange ? [currentRange] : [])));
}

/** Remove both preview-search highlights. */
export function clearPreviewHighlights(): void {
  if (!highlightsSupported()) return;
  CSS.highlights.delete(HIGHLIGHT_ALL);
  CSS.highlights.delete(HIGHLIGHT_CURRENT);
}
