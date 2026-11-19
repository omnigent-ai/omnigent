// Maps rendered Markdown preview text back to raw-source offsets, so Preview
// comments anchor like the editor's: a rendered quote plus the raw range from
// the first to the last selected character.

const SOURCE_START = "data-source-start";
const SOURCE_END = "data-source-end";
const SOURCE_BLOCK = "data-source-block";
const SPAN_SELECTOR = `span[${SOURCE_START}]`;

// Elements whose text is a separate block in the editor's comment quote, which
// joins text from different blocks with one newline.
const BLOCK_TAGS = new Set([
  "p",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "li",
  "td",
  "th",
  "pre",
  "blockquote",
  "dt",
  "dd",
  "summary",
  "div",
]);

// How far ahead one rendered character may sit from the previous match, e.g.
// past a quote marker, list indent or entity that the renderer dropped.
const ALIGN_WINDOW = 24;

interface HastPoint {
  offset?: number;
}
interface HastPosition {
  start: HastPoint;
  end: HastPoint;
}
interface HastText {
  type: "text";
  value: string;
  position?: HastPosition;
}
interface HastElement {
  type: "element";
  tagName: string;
  properties?: Record<string, unknown>;
  children: HastChild[];
  position?: HastPosition;
}
interface HastParent {
  type: string;
  children: HastChild[];
}
type HastChild = HastText | HastElement | { type: string; children?: HastChild[] };

function classNames(element: HastElement): string[] {
  const value = element.properties?.className;
  if (Array.isArray(value)) return value.map(String);
  return typeof value === "string" ? value.split(/\s+/) : [];
}

// Math is rendered by KaTeX and Mermaid fences by MermaidPreview, both from the
// element's plain text, and an alert's title is generated, so their text is
// left unwrapped and is not commentable.
function isOpaque(element: HastElement): boolean {
  return classNames(element).some((name) => {
    const lower = name.toLowerCase();
    return (
      lower === "language-math" || lower === "language-mermaid" || lower === "markdown-alert-title"
    );
  });
}

// rehype-github-alerts rebuilds a `> [!NOTE]` quote without positions, so its
// rebuilt text is located within the source between the neighbouring blocks.
function isAlert(element: HastElement): boolean {
  return classNames(element).includes("markdown-alert");
}

function nextStart(children: HastChild[], after: number): number | null {
  for (let index = after + 1; index < children.length; index += 1) {
    const child = children[index];
    const range = "position" in child ? rangeOf(child.position as HastPosition) : null;
    if (range) return range[0];
  }
  return null;
}

function rangeOf(position: HastPosition | undefined): [number, number] | null {
  const start = position?.start.offset;
  const end = position?.end.offset;
  return typeof start === "number" && typeof end === "number" && end > start ? [start, end] : null;
}

function wrap(text: HastText, range: [number, number], block: number): HastElement {
  return {
    type: "element",
    tagName: "span",
    properties: { dataSourceStart: range[0], dataSourceEnd: range[1], dataSourceBlock: block },
    children: [text],
  };
}

// Text that a plugin rebuilt (emoji shortcodes) or that never had a position (a
// code block's body) is located in order within its parent element's range.
function locate(
  source: string,
  value: string,
  cursor: number,
  limit: number,
): [number, number] | null {
  const exact = source.indexOf(value, cursor);
  if (exact !== -1 && exact + value.length <= limit) return [exact, exact + value.length];
  const firstLine = value.split("\n", 1)[0];
  const line = firstLine.trim() ? source.indexOf(firstLine, cursor) : -1;
  if (line !== -1 && line < limit) return [line, limit];
  return null;
}

/**
 * Wraps each placeable text node in a span carrying its raw-source range.
 * Run after sanitize (so only this plugin sets the attributes) and before KaTeX.
 */
export function rehypeSourceOffsets() {
  return (tree: HastParent, file?: { value?: unknown }) => {
    const source = typeof file?.value === "string" ? file.value : "";
    let blockCount = 0;
    const visit = (
      parent: HastParent,
      block: number,
      parentRange: [number, number] | null,
      inAlert: boolean,
    ) => {
      let cursor = parentRange?.[0] ?? 0;
      // Unplaced text waiting for the next placed sibling to bound its range.
      let unplaced: { index: number; start: number }[] = [];
      const place = (end: number) => {
        for (const { index, start } of unplaced) {
          if (end > start) {
            parent.children[index] = wrap(parent.children[index] as HastText, [start, end], block);
          }
        }
        unplaced = [];
      };
      for (let index = 0; index < parent.children.length; index += 1) {
        const child = parent.children[index];
        if (child.type === "element") {
          const element = child as HastElement;
          const range = rangeOf(element.position);
          if (range) {
            place(range[0]);
            cursor = range[1];
          }
          if (isOpaque(element)) continue;
          const childBlock = BLOCK_TAGS.has(element.tagName) ? ++blockCount : block;
          const alert = inAlert || isAlert(element);
          let childRange = range;
          if (!childRange && alert && source) {
            const end = nextStart(parent.children, index) ?? parentRange?.[1] ?? source.length;
            childRange = inAlert ? parentRange : end > cursor ? [cursor, end] : null;
          }
          visit(element, childBlock, childRange, alert);
        } else if (child.type === "text") {
          const text = child as HastText;
          if (!text.value.trim()) continue;
          let range = rangeOf(text.position);
          if (!range && parentRange && source) {
            range = locate(source, text.value, cursor, parentRange[1]);
            if (!range) {
              unplaced.push({ index, start: cursor });
              continue;
            }
          }
          if (!range) continue;
          place(range[0]);
          parent.children[index] = wrap(text, range, block);
          cursor = range[1];
        } else if ("children" in child && child.children) {
          visit(child as HastParent, block, null, inAlert);
        }
      }
      if (parentRange) place(parentRange[1]);
    };
    visit(tree, 0, null, false);
  };
}

/**
 * Source offset of each character of `text`, which renders `source[start, end)`.
 * Characters are matched in order; one with no match nearby (an emoji glyph,
 * say) takes the position of the next unmatched source character.
 */
export function alignTextToSource(
  text: string,
  source: string,
  start: number,
  end: number,
): number[] {
  const offsets = new Array<number>(text.length);
  if (source.slice(start, end) === text) {
    for (let index = 0; index < text.length; index += 1) offsets[index] = start + index;
    return offsets;
  }
  let cursor = start;
  for (let index = 0; index < text.length; index += 1) {
    const limit = index === 0 ? end : Math.min(end, cursor + ALIGN_WINDOW);
    let found = -1;
    for (let position = cursor; position < limit; position += 1) {
      if (source[position] === text[index]) {
        found = position;
        break;
      }
    }
    if (found === -1) {
      offsets[index] = Math.min(cursor, end - 1);
    } else {
      offsets[index] = found;
      cursor = found + 1;
    }
  }
  return offsets;
}

interface SourceSpan {
  element: HTMLElement;
  text: Text;
  block: string;
  offsets: number[];
}

const alignmentCache = new WeakMap<
  HTMLElement,
  { source: string; start: number; end: number; value: string; offsets: number[] }
>();

function readSpan(element: HTMLElement, source: string): SourceSpan | null {
  const text = element.firstChild;
  if (!(text instanceof Text) || element.childNodes.length !== 1) return null;
  const start = Number(element.getAttribute(SOURCE_START));
  const end = Number(element.getAttribute(SOURCE_END));
  if (!Number.isInteger(start) || !Number.isInteger(end) || end <= start || end > source.length) {
    return null;
  }
  const value = text.data;
  const cached = alignmentCache.get(element);
  let offsets: number[];
  if (
    cached &&
    cached.source === source &&
    cached.start === start &&
    cached.end === end &&
    cached.value === value
  ) {
    offsets = cached.offsets;
  } else {
    offsets = alignTextToSource(value, source, start, end);
    alignmentCache.set(element, { source, start, end, value, offsets });
  }
  return { element, text, block: element.getAttribute(SOURCE_BLOCK) ?? "", offsets };
}

function sourceSpans(root: HTMLElement, source: string): SourceSpan[] {
  const spans: SourceSpan[] = [];
  root.querySelectorAll<HTMLElement>(SPAN_SELECTOR).forEach((element) => {
    const span = readSpan(element, source);
    if (span) spans.push(span);
  });
  return spans;
}

/** Selected [from, to) character slice of a span's text, or null when none is. */
function selectedSlice(range: Range, text: Text): [number, number] | null {
  const length = text.data.length;
  const from =
    range.startContainer === text ? range.startOffset : range.comparePoint(text, 0) >= 0 ? 0 : -1;
  const to =
    range.endContainer === text
      ? range.endOffset
      : range.comparePoint(text, length) <= 0
        ? length
        : -1;
  if (from === -1 || to === -1 || from >= to) return null;
  return [from, to];
}

export interface PreviewSourceSelection {
  start_index: number;
  end_index: number;
  anchor_content: string;
}

/**
 * Raw-source anchor for a selection in the rendered preview, or null when it
 * covers no mapped text. Leading and trailing whitespace is dropped, and text
 * from different blocks is joined with one newline, as in the editor's quote.
 */
export function previewSelectionToSource(
  range: Range,
  root: HTMLElement,
  source: string,
): PreviewSourceSelection | null {
  const pieces: { span: SourceSpan; from: number; to: number }[] = [];
  for (const span of sourceSpans(root, source)) {
    if (!range.intersectsNode(span.text)) continue;
    const slice = selectedSlice(range, span.text);
    if (slice) pieces.push({ span, from: slice[0], to: slice[1] });
  }
  // Trim whitespace at both ends, dropping pieces that become empty.
  while (pieces.length > 0) {
    const first = pieces[0];
    while (first.from < first.to && /\s/.test(first.span.text.data[first.from])) first.from += 1;
    if (first.from < first.to) break;
    pieces.shift();
  }
  while (pieces.length > 0) {
    const last = pieces[pieces.length - 1];
    while (last.to > last.from && /\s/.test(last.span.text.data[last.to - 1])) last.to -= 1;
    if (last.to > last.from) break;
    pieces.pop();
  }
  if (pieces.length === 0) return null;

  let anchor_content = "";
  pieces.forEach((piece, index) => {
    if (index > 0 && piece.span.block !== pieces[index - 1].span.block) anchor_content += "\n";
    anchor_content += piece.span.text.data.slice(piece.from, piece.to);
  });
  const first = pieces[0];
  const last = pieces[pieces.length - 1];
  return {
    start_index: first.span.offsets[first.from],
    end_index: last.span.offsets[last.to - 1] + 1,
    anchor_content,
  };
}

/** Raw-source offset of a caret position in the preview, or null off mapped text. */
export function previewPointToSource(node: Node, offset: number, source: string): number | null {
  const element = node instanceof Text ? node.parentElement : null;
  if (!element?.matches(SPAN_SELECTOR)) return null;
  const span = readSpan(element, source);
  if (!span || span.offsets.length === 0) return null;
  return span.offsets[Math.min(offset, span.offsets.length - 1)];
}

/** DOM ranges covering the rendered text of raw-source range [start, end). */
export function previewRangesForSource(
  root: HTMLElement,
  source: string,
  start: number,
  end: number,
): Range[] {
  const ranges: Range[] = [];
  if (end <= start) return ranges;
  for (const span of sourceSpans(root, source)) {
    const { offsets } = span;
    let from = 0;
    while (from < offsets.length && offsets[from] < start) from += 1;
    let to = from;
    while (to < offsets.length && offsets[to] < end) to += 1;
    if (from >= to) continue;
    const range = root.ownerDocument.createRange();
    range.setStart(span.text, from);
    range.setEnd(span.text, to);
    ranges.push(range);
  }
  return ranges;
}
