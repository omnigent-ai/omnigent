// Maps rendered Markdown preview text back to raw-source offsets, so Preview
// comments anchor like the editor's: a rendered quote plus the raw range from
// the first to the last selected character.

const SOURCE_START = "data-source-start";
const SOURCE_END = "data-source-end";
const SOURCE_BLOCK = "data-source-block";
const SOURCE_VERBATIM = "data-source-verbatim";
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

// Containers whose whitespace children separate blocks (or that React drops), so
// their whitespace is not part of any quote.
const CONTAINER_TAGS = new Set([
  "table",
  "thead",
  "tbody",
  "tfoot",
  "tr",
  "ul",
  "ol",
  "dl",
  "blockquote",
  "div",
  "details",
]);

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

function wrap(
  text: HastText,
  range: [number, number],
  block: number,
  verbatim: boolean,
): HastElement {
  const properties: Record<string, unknown> = {
    dataSourceStart: range[0],
    dataSourceEnd: range[1],
    dataSourceBlock: block,
  };
  // Code shows its source as written: no entities, escapes or emoji shortcodes.
  if (verbatim) properties.dataSourceVerbatim = "true";
  return { type: "element", tagName: "span", properties, children: [text] };
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

function holdsInlineWhitespace(parent: HastParent): boolean {
  return parent.type === "element" && !CONTAINER_TAGS.has((parent as HastElement).tagName);
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
      inCode: boolean,
    ): number => {
      let cursor = parentRange?.[0] ?? 0;
      // Unplaced text waiting for the next placed sibling to bound its range.
      let unplaced: { index: number; start: number }[] = [];
      const place = (end: number) => {
        for (const { index, start } of unplaced) {
          if (end > start) {
            const text = parent.children[index] as HastText;
            parent.children[index] = wrap(text, [start, end], block, inCode);
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
            // Inside an alert, siblings share its range, so each starts where the last ended.
            const end = inAlert
              ? parentRange?.[1]
              : (nextStart(parent.children, index) ?? parentRange?.[1] ?? source.length);
            childRange = end !== undefined && end > cursor ? [cursor, end] : null;
          }
          const code = inCode || element.tagName === "code" || element.tagName === "pre";
          const consumed = visit(element, childBlock, childRange, alert, code);
          if (!range && childRange) cursor = consumed;
        } else if (child.type === "text") {
          const text = child as HastText;
          let range = rangeOf(text.position);
          // Generated separators have no position; positioned whitespace (a space
          // between two bold words, say) is part of the selected quote.
          if (!text.value.trim() && (!range || !holdsInlineWhitespace(parent))) continue;
          if (!range && parentRange && source) {
            range = locate(source, text.value, cursor, parentRange[1]);
            if (!range) {
              unplaced.push({ index, start: cursor });
              continue;
            }
          }
          if (!range) continue;
          place(range[0]);
          parent.children[index] = wrap(text, range, block, inCode);
          cursor = range[1];
        } else if ("children" in child && child.children) {
          visit(child as HastParent, block, null, inAlert, inCode);
        }
      }
      if (parentRange) place(parentRange[1]);
      return cursor;
    };
    visit(tree, 0, null, false, false);
  };
}

// Character references as CommonMark recognizes them; sticky, so it matches at
// `lastIndex` only.
const ENTITY_PATTERN = /&(?:#\d{1,7}|#x[\da-f]{1,6}|[a-z][\da-z]*);/iy;
// remark-emoji's shortcode syntax, and the glyph it renders one as.
const SHORTCODE_PATTERN = /:\+1:|:-1:|:[\w-]+:/y;
const EMOJI_PATTERN =
  /(?:[#*0-9]\ufe0f?\u20e3|\p{Extended_Pictographic}|\p{Emoji_Modifier}|\p{Regional_Indicator}|\u200d|\ufe0f|[\u{e0020}-\u{e007f}])+/uy;
const ESCAPABLE = /[!-/:-@[-`{-~]/;

let entityDecoder: HTMLTextAreaElement | null = null;

/** Decoded text of a character reference, or null when Markdown keeps it literally. */
function decodeEntity(entity: string): string | null {
  // The pattern admits only `&name;`/`&#n;` text, so no markup is parsed here.
  entityDecoder ??= document.createElement("textarea");
  entityDecoder.innerHTML = entity;
  const decoded = entityDecoder.value;
  // HTML also decodes a known prefix (`&ampx;` as `&x;`), which Markdown does not.
  if (decoded === entity || (decoded.length > 1 && decoded.endsWith(";"))) return null;
  return decoded;
}

/** Raw-source range [starts[i], ends[i]) that rendered character `i` came from. */
export interface TextAlignment {
  starts: number[];
  ends: number[];
}

/**
 * Source range of each character of `text`, which renders `source[start, end)`,
 * or null when the text can't be matched to it. Prose may decode entities,
 * escapes and emoji shortcodes; `verbatim` (code) text matches as written. A
 * line's trailing spaces and its indent or quote markers may be dropped, and
 * whitespace with no source takes an empty range.
 */
export function alignTextToSource(
  text: string,
  source: string,
  start: number,
  end: number,
  verbatim = false,
): TextAlignment | null {
  const starts = new Array<number>(text.length);
  const ends = new Array<number>(text.length);
  if (source.slice(start, end) === text) {
    for (let index = 0; index < text.length; index += 1) {
      starts[index] = start + index;
      ends[index] = start + index + 1;
    }
    return { starts, ends };
  }
  // Rendered and source lengths of the construct at `position` that renders
  // text from `index`, or null when none does.
  const match = (index: number, position: number): [number, number] | null => {
    const char = source[position];
    if (!verbatim && char === "\\") {
      const escaped = source[position + 1];
      if (position + 1 < end && ESCAPABLE.test(escaped) && text[index] === escaped) return [1, 2];
    }
    if (!verbatim && char === "&") {
      ENTITY_PATTERN.lastIndex = position;
      const entity = ENTITY_PATTERN.exec(source)?.[0];
      const decoded = entity && position + entity.length <= end ? decodeEntity(entity) : null;
      if (entity && decoded !== null) {
        return text.startsWith(decoded, index) ? [decoded.length, entity.length] : null;
      }
    }
    if (!verbatim && char === ":") {
      SHORTCODE_PATTERN.lastIndex = position;
      const shortcode = SHORTCODE_PATTERN.exec(source)?.[0];
      EMOJI_PATTERN.lastIndex = index;
      const glyph =
        shortcode && position + shortcode.length <= end && EMOJI_PATTERN.exec(text)?.[0];
      if (shortcode && glyph) return [glyph.length, shortcode.length];
    }
    return char === text[index] ? [1, 1] : null;
  };
  const zeroWidth = (index: number, at: number) => {
    starts[index] = at;
    ends[index] = at;
  };
  // Syntax before the first visible character (a heading's `#`) renders nothing,
  // and whitespace before it may be generated (after a task's checkbox).
  const lead = text.search(/\S/);
  let cursor = start;
  if (lead > -1) while (cursor < end && !match(lead, cursor)) cursor += 1;
  for (let index = 0; index < lead; index += 1) zeroWidth(index, cursor);
  let index = Math.max(lead, 0);
  let lineStart = false;
  while (index < text.length) {
    const step = cursor < end ? match(index, cursor) : null;
    if (step) {
      const [rendered, raw] = step;
      for (let offset = 0; offset < rendered; offset += 1) {
        starts[index + offset] = cursor;
        ends[index + offset] = cursor + raw;
      }
      index += rendered;
      cursor += raw;
      lineStart = text[index - 1] === "\n";
      continue;
    }
    const skipped = source[cursor];
    const dropped =
      cursor < end &&
      ((text[index] === "\n" && (skipped === " " || skipped === "\t" || skipped === "\r")) ||
        (lineStart && (skipped === " " || skipped === "\t" || skipped === ">")));
    if (dropped) {
      cursor += 1;
    } else if (/\s/.test(text[index])) {
      // Whitespace the renderer added, e.g. before a footnote's back link.
      zeroWidth(index, cursor);
      index += 1;
    } else {
      return null;
    }
  }
  return { starts, ends };
}

interface SourceSpan {
  element: HTMLElement;
  text: Text;
  block: string;
  start: number;
  end: number;
  /** Null when the text couldn't be matched to its source, so no part of it maps. */
  alignment: TextAlignment | null;
}

const alignmentCache = new WeakMap<
  HTMLElement,
  {
    source: string;
    start: number;
    end: number;
    value: string;
    verbatim: boolean;
    alignment: TextAlignment | null;
  }
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
  const verbatim = element.hasAttribute(SOURCE_VERBATIM);
  const cached = alignmentCache.get(element);
  let alignment: TextAlignment | null;
  if (
    cached &&
    cached.source === source &&
    cached.start === start &&
    cached.end === end &&
    cached.value === value &&
    cached.verbatim === verbatim
  ) {
    alignment = cached.alignment;
  } else {
    alignment = alignTextToSource(value, source, start, end, verbatim);
    alignmentCache.set(element, { source, start, end, value, verbatim, alignment });
  }
  const block = element.getAttribute(SOURCE_BLOCK) ?? "";
  return { element, text, block, start, end, alignment };
}

function mappedSpan(text: Text, source: string): SourceSpan | null {
  const element = text.parentElement;
  return element?.matches(SPAN_SELECTOR) ? readSpan(element, source) : null;
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
  // `unmapped` counts selected text with no source range (rendered math, an
  // alert title) before each piece; the editor's quote would include it.
  const pieces: { span: SourceSpan; from: number; to: number; unmapped: number }[] = [];
  let unmapped = 0;
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node as Text;
    if (!range.intersectsNode(text)) continue;
    const slice = selectedSlice(range, text);
    if (!slice) continue;
    const span = mappedSpan(text, source);
    if (span) pieces.push({ span, from: slice[0], to: slice[1], unmapped });
    else if (text.data.slice(slice[0], slice[1]).trim()) unmapped += 1;
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
  const first = pieces[0];
  const last = pieces[pieces.length - 1];
  const firstAlignment = first.span.alignment;
  const lastAlignment = last.span.alignment;
  if (first.unmapped !== last.unmapped || !firstAlignment || !lastAlignment) return null;

  let anchor_content = "";
  pieces.forEach((piece, index) => {
    if (index > 0 && piece.span.block !== pieces[index - 1].span.block) anchor_content += "\n";
    anchor_content += piece.span.text.data.slice(piece.from, piece.to);
  });
  return {
    start_index: firstAlignment.starts[first.from],
    end_index: lastAlignment.ends[last.to - 1],
    anchor_content,
  };
}

/** Raw-source offset of a caret position in the preview, or null off mapped text. */
export function previewPointToSource(node: Node, offset: number, source: string): number | null {
  const alignment = node instanceof Text ? mappedSpan(node, source)?.alignment : null;
  if (!alignment || alignment.starts.length === 0) return null;
  const { starts, ends } = alignment;
  // A caret after a node's last character sits just past that character's source.
  return offset >= starts.length ? ends[ends.length - 1] : starts[offset];
}

/** DOM ranges covering the rendered text of raw-source range [start, end). */
export function previewRangesForSource(
  root: HTMLElement,
  source: string,
  start: number,
  end: number,
): Range[] {
  return previewRangeLookup(root, source)(start, end);
}

/** Reads the preview's mapped text once, for finding the ranges of many comments. */
export function previewRangeLookup(
  root: HTMLElement,
  source: string,
): (start: number, end: number) => Range[] {
  const spans = sourceSpans(root, source);
  return (start, end) => rangesInSpans(root, spans, start, end);
}

function rangesInSpans(
  root: HTMLElement,
  spans: SourceSpan[],
  start: number,
  end: number,
): Range[] {
  const ranges: Range[] = [];
  if (end <= start) return ranges;
  const textRange = (text: Text, from: number, to: number) => {
    const range = root.ownerDocument.createRange();
    range.setStart(text, from);
    range.setEnd(text, to);
    return range;
  };
  for (const span of spans) {
    if (!span.alignment) {
      // Unmatched text is shown only when the comment covers all of it.
      if (start <= span.start && span.end <= end) {
        ranges.push(textRange(span.text, 0, span.text.length));
      }
      continue;
    }
    const { starts, ends } = span.alignment;
    let from = 0;
    while (from < ends.length && ends[from] <= start) from += 1;
    let to = from;
    while (to < starts.length && starts[to] < end) to += 1;
    if (from < to) ranges.push(textRange(span.text, from, to));
  }
  return ranges;
}
