// Find-in-page matching over React-owned rendered text.
//
// Highlights must not mutate the node tree (that would fight React's
// reconciliation), so matches are located as DOM `Range`s and painted with the
// CSS Custom Highlight API, which overlays styling without touching the nodes.
//
// Text is flattened across inline nodes so a term split by formatting (e.g.
// `<em>`) still matches, while a block boundary inserts a separator so a match
// never spans two blocks.

// Block-level tags whose boundary breaks a run of visible text. A match may
// span inline formatting within one block, but never cross into another.
export const BLOCK_TAGS: ReadonlySet<string> = new Set([
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
export function lowerPreservingLength(s: string): string {
  let out = "";
  for (const ch of s) {
    const lower = ch.toLowerCase();
    out += lower.length === ch.length ? lower : ch;
  }
  return out;
}

/**
 * An element whose rendered text differs from what it stands for (a shortened
 * file label, a media player for a link) carries that text here. Find searches
 * the attribute in its place and matches the whole element.
 */
export const FIND_SOURCE_TEXT_ATTRIBUTE = "data-thread-find-source";

interface TextSegment {
  /** A text node, or an element searched as its source text. */
  node: Text | Element;
  /** Offset of this run's text within the flattened string. */
  visibleFrom: number;
  length: number;
}

export interface TextRangeSearchOptions {
  /** Text under an element matching this selector (e.g. control labels) is not searched. */
  skipSelector?: string;
}

interface FlattenedText {
  text: string;
  segments: TextSegment[];
  /** Flattened-string indices of the newlines that separate blocks. */
  separators: number[];
}

function flattenText(container: Element, { skipSelector }: TextRangeSearchOptions): FlattenedText {
  const flattened: FlattenedText = { text: "", segments: [], separators: [] };
  let previousBlock: Element | null = null;
  const append = (node: Text | Element, value: string, block: Element) => {
    if (!value) return;
    if (previousBlock !== null && block !== previousBlock) {
      flattened.separators.push(flattened.text.length);
      flattened.text += "\n";
    }
    previousBlock = block;
    flattened.segments.push({ node, visibleFrom: flattened.text.length, length: value.length });
    flattened.text += value;
  };
  const visit = (parent: Element, block: Element) => {
    for (let node = parent.firstChild; node; node = node.nextSibling) {
      if (node.nodeType === Node.TEXT_NODE) {
        append(node as Text, node.nodeValue ?? "", block);
        continue;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      const element = node as Element;
      if (skipSelector && element.matches(skipSelector)) continue;
      const source = element.getAttribute(FIND_SOURCE_TEXT_ATTRIBUTE);
      if (source !== null) {
        append(element, source, block);
        continue;
      }
      visit(element, BLOCK_TAGS.has(element.tagName) || isCodeLine(element) ? element : block);
    }
  };
  visit(container, container);
  return flattened;
}

/** A highlighted code block draws each line as its own block-level span. */
function isCodeLine(element: Element): boolean {
  const code = element.parentElement;
  return code?.tagName === "CODE" && code.parentElement?.tagName === "PRE";
}

/**
 * The text find searches in `container`: rendered text in document order, with
 * a newline between consecutive runs that sit in different blocks.
 */
export function searchableText(container: Element, options: TextRangeSearchOptions = {}): string {
  return flattenText(container, options).text;
}

/**
 * Case-insensitive plain-text matches of `query` within `container`, returned as
 * DOM Ranges ready to paint. Matches span adjacent text nodes inside one block
 * but never cross a block boundary. Returns [] for an empty query or when no
 * text matches.
 */
export function findTextRanges(
  container: Element,
  query: string,
  options: TextRangeSearchOptions = {},
): Range[] {
  const q = lowerPreservingLength(query);
  if (!q) return [];

  const { text, segments, separators } = flattenText(container, options);
  const haystack = lowerPreservingLength(text);
  const ranges: Range[] = [];
  let idx = haystack.indexOf(q);
  while (idx !== -1) {
    const start = idx;
    const end = idx + q.length;
    idx = haystack.indexOf(q, end);

    // A match touching a block separator spans two blocks — skip it.
    if (separators.some((s) => s >= start && s < end)) continue;

    const startSeg = segments.find(
      (s) => s.visibleFrom <= start && start < s.visibleFrom + s.length,
    );
    const endSeg = segments.find((s) => s.visibleFrom < end && end <= s.visibleFrom + s.length);
    if (!startSeg || !endSeg) continue;

    const range = document.createRange();
    if (startSeg.node.nodeType === Node.TEXT_NODE) {
      range.setStart(startSeg.node, start - startSeg.visibleFrom);
    } else {
      range.setStartBefore(startSeg.node);
    }
    if (endSeg.node.nodeType === Node.TEXT_NODE) {
      range.setEnd(endSeg.node, end - endSeg.visibleFrom);
    } else {
      range.setEndAfter(endSeg.node);
    }
    ranges.push(range);
  }
  return ranges;
}

/** The pair of highlight registry names one find surface paints under. */
export interface SearchHighlightNames {
  all: string;
  current: string;
}

/** True when the browser supports the CSS Custom Highlight API. */
export function searchHighlightsSupported(): boolean {
  return typeof CSS !== "undefined" && !!CSS.highlights && typeof Highlight !== "undefined";
}

/**
 * Paint `ranges` as the "all matches" highlight, with `current` promoted to the
 * "current match" highlight. No-op where the API is missing.
 */
export function paintSearchHighlights(
  names: SearchHighlightNames,
  ranges: readonly Range[],
  current: Range | null,
): void {
  if (!searchHighlightsSupported()) return;
  const rest = ranges.filter((range) => range !== current);
  CSS.highlights.set(names.all, new Highlight(...rest));
  CSS.highlights.set(names.current, new Highlight(...(current ? [current] : [])));
}

/** Remove both highlights of one find surface. */
export function clearSearchHighlights(names: SearchHighlightNames): void {
  if (!searchHighlightsSupported()) return;
  CSS.highlights.delete(names.all);
  CSS.highlights.delete(names.current);
}
