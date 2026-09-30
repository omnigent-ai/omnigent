/**
 * Tokenizer for the composer's live Markdown highlight layer.
 *
 * The layer only restyles the draft; it never changes its text. So the
 * tokenizer maps the draft onto styled ranges that cover it exactly, split into
 * blocks (paragraphs and fenced code blocks) whose parses are cached by text so
 * a keystroke re-parses only the paragraph it lands in.
 *
 * Supported syntax follows CommonMark (as micromark, the transcript's parser,
 * implements it) for the subset the layer styles:
 * - `*em*` / `_em_` and `**strong**` / `__strong__`, using the delimiter-run
 *   flanking rules, so `snake_case_names` and `file_name.py` stay plain while
 *   `*` may match intraword.
 * - `` `code` `` spans (any matching backtick-run length); nothing inside is
 *   parsed further.
 * - ``` / ~~~ fenced code blocks. An unclosed fence styles through to the end
 *   of the draft, as code editors do while the closing fence is still unwritten.
 * - Backslash-escaped punctuation (`\_`, `\*`, `` \` ``) is never a marker.
 *
 * Unmatched inline markers stay unstyled. Emphasis and code spans never cross a
 * blank line or a fence. LF, CRLF and a bare CR all end a line; offsets always
 * index the original text. A paragraph longer than `MAX_PARAGRAPH_LINES` (a
 * pasted log, say) resolves its code spans across the whole paragraph first,
 * then parses emphasis line by line, so a keystroke re-parses one line: only
 * emphasis that crosses a line inside such a paragraph goes unstyled, and
 * nothing inside code is ever styled.
 */

export const MD_ITALIC = 1;
export const MD_BOLD = 2;
export const MD_CODE = 4;
export const MD_FENCE = 8;
export const MD_MARKER = 16;
/** A fence's info string (```ts): shown dimmed, never hidden. */
export const MD_INFO = 128;
/** An unclosed fence's backticks: shown dimmed, like other unclosed markers. */
export const MD_OPEN_FENCE = 256;
/** ~~Strikethrough~~ text (GFM: one or two tildes). */
export const MD_STRIKE = 512;
/** A link's text, in `[text](url)`. */
export const MD_LINK = 1024;
/** A link's destination and title: shown dimmed, never hidden, so it stays readable. */
export const MD_LINK_URL = 2048;
/** Text inside a `>` block quote: dimmed, with a bar beside its lines. */
export const MD_QUOTE = 4096;

/** One run of identically styled characters, as offsets into its block's text. */
export interface MarkdownSegment {
  start: number;
  end: number;
  flags: number;
}

/** A contiguous slice of the draft; blocks tile the draft end to end. */
/**
 * One closed, styled token (emphasis, a code span, a fenced block), block-relative.
 * A caret or selection touching `[start, end]` (boundaries included) reveals
 * exactly its `markers`.
 */
export interface MarkdownToken {
  start: number;
  end: number;
  markers: readonly RevealRange[];
}

export interface MarkdownBlock {
  start: number;
  text: string;
  /** Cached per block text, so an unchanged block keeps the same array. */
  segments: readonly MarkdownSegment[];
  tokens: readonly MarkdownToken[];
  /** Set on a fenced code block, which is one block of its own. */
  fence?: MarkdownFence;
}

/** A fenced code block's facts, block-relative, for styling and keyboard handling. */
export interface MarkdownFence {
  /** No closing fence: the block runs to the end of the draft or of its quote. */
  open: boolean;
  /** The opening fence's run (``` or ~~~), to close an open block with. */
  run: string;
  /** Quote depth: every line of a block inside a quote starts with this many `>`. */
  depth: number;
  /** Start of the closing fence line; null when open. */
  closeLine: number | null;
  /**
   * What starts a new line of the block: its containers' `>` markers and list
   * item indents (`>   ` for a fence in `> - `), or "" for a bare block.
   */
  prefix: string;
  /** Each line's text start, after its containers' prefix. */
  lines: readonly number[];
}

/** A fenced block's container: a quote, or a list item whose text is `indent` columns in. */
type FenceContainer = { quote: true } | { quote: false; indent: number };

/** A block's parse: segments that break at every marker's edges, and its tokens. */
interface ParsedBlock {
  segments: readonly MarkdownSegment[];
  tokens: readonly MarkdownToken[];
}

export interface ComposerMarkdown {
  blocks: MarkdownBlock[];
  /** Whether any character carries a style (so the layer is worth mounting). */
  styled: boolean;
}

const FENCE_OPEN_RE = /^ {0,3}(`{3,}(?=[^`]*$)|~{3,})/;
const FENCE_CLOSE_RE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;
const BLANK_LINE_RE = /^[ \t]*$/;
// Lines that end a quote's lazy continuation: CommonMark lets these interrupt a
// paragraph (fences are matched separately; HTML blocks aren't recognized).
const HEADING_RE = /^ {0,3}#{1,6}(?:[ \t]|$)/;
const THEMATIC_BREAK_RE = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const LIST_ITEM_RE = /^ {0,3}(?:[-+*]|1[.)])[ \t]+\S/;
const SETEXT_UNDERLINE_RE = /^(?:=+|-+)[ \t]*$/;
const LIST_MARKER_RE = /^(?:[-+*]|(\d{1,9})[.)])(?=[ \t]|$)/;
/** Past this many lines, a paragraph is parsed (and cached) one line at a time. */
export const MAX_PARAGRAPH_LINES = 100;

const WHITESPACE_RE = /\s/u;
const PUNCTUATION_RE = /[\p{P}\p{S}]/u;
const ASCII_PUNCTUATION_RE = /[!-/:-@[-`{-~]/;

const LF = 0x0a;
const CR = 0x0d;
const BACKSLASH = 0x5c;
const BACKTICK = 0x60;
const STAR = 0x2a;
const TILDE = 0x7e;
const OPEN_BRACKET = 0x5b;
const CLOSE_BRACKET = 0x5d;
const OPEN_PAREN = 0x28;
const CLOSE_PAREN = 0x29;
const BANG = 0x21;
const UNDERSCORE = 0x5f;

/**
 * A two-generation cache: entries read or written since the last rotation are
 * kept, older ones age out one rotation later. Rotating on size keeps memory
 * bounded without dropping the entries the current draft still uses.
 */
export class GenerationalCache<V> {
  private current = new Map<string, V>();
  private previous = new Map<string, V>();
  private readonly limit: number;

  constructor(limit: number) {
    this.limit = limit;
  }

  get(key: string): V | undefined {
    const hit = this.current.get(key);
    if (hit !== undefined) return hit;
    const aged = this.previous.get(key);
    if (aged !== undefined) this.set(key, aged);
    return aged;
  }

  set(key: string, value: V): void {
    if (this.current.size >= this.limit) {
      this.previous = this.current;
      this.current = new Map();
    }
    this.current.set(key, value);
  }
}

const paragraphCache = new GenerationalCache<ParsedBlock>(10000);
const fenceCache = new GenerationalCache<ParsedBlock & { fence: MarkdownFence }>(1000);

/** Split a draft into blocks and style each; see the module docs for the rules. */
export function tokenizeComposerMarkdown(text: string): ComposerMarkdown {
  const blocks: MarkdownBlock[] = [];
  const cr = text.includes("\r");
  let styled = false;
  const pushBlock = (start: number, blockText: string, parsed: ParsedBlock) => {
    const { segments } = parsed;
    if (segments.length > 1 || (segments.length === 1 && segments[0].flags !== 0)) styled = true;
    blocks.push({ start, text: blockText, segments, tokens: parsed.tokens });
  };
  let paragraphLines = 0;
  const pushParagraph = (start: number, end: number) => {
    if (end <= start) return;
    const paragraph = text.slice(start, end);
    if (paragraphLines > MAX_PARAGRAPH_LINES) {
      const quoted = quotePrefix(paragraph.slice(0, lineEndAt(paragraph, 0, cr))) !== null;
      const code = findCodePieces(paragraph);
      const { pieces } = code;
      let first = 0;
      let firstToken = 0;
      for (let line = 0; line < paragraph.length;) {
        const next = nextLineStart(paragraph, lineEndAt(paragraph, line, cr));
        while (first < pieces.length && pieces[first].end <= line) first++;
        while (firstToken < code.tokens.length && code.tokens[firstToken].end < line) firstToken++;
        const lineText = paragraph.slice(line, next);
        const parsed = lineSegments(lineText, line, next, pieces, first, quoted);
        // A code span crossing lines joins every line it touches, line-relative.
        const tokens = [...parsed.tokens];
        for (let t = firstToken; t < code.tokens.length && code.tokens[t].start < next; t++)
          tokens.push(shiftToken(code.tokens[t], -line));
        pushBlock(start + line, lineText, { segments: parsed.segments, tokens });
        line = next;
      }
    } else pushBlock(start, paragraph, paragraphSegments(paragraph));
    paragraphLines = 0;
    paragraphQuote = 0;
  };

  let paragraphStart = 0;
  // The open paragraph's quote depth (0: a plain paragraph) and whether it has lines.
  let paragraphQuote = 0;
  let paragraphOpen = false;
  // Whether the open quote paragraph can continue lazily: only a paragraph can.
  let lazyOk = false;
  // A list item open in a quote: its depth, and its text's column from the
  // quote's text, so lines indented that far are read as the item's own.
  let item = null as { depth: number; column: number } | null;
  // The quotes and list items the last line left open, for where a fence sits.
  let open: FenceContainer[] = [];
  let pos = 0;
  while (pos < text.length) {
    const lineEnd = lineEndAt(text, pos, cr);
    const next = nextLineStart(text, lineEnd);
    const line = text.slice(pos, lineEnd);
    // Containers first: a quote's `>` markers, then what the line holds.
    const quote = quotePrefix(line);
    const content = quote ? line.slice(quote.contentStart) : line;
    // A fence, bare or inside the quotes and list items open here.
    const containers = lineContainers(line, open, paragraphOpen);
    open = containers.open;
    if (containers.fence) {
      pushParagraph(paragraphStart, pos);
      const fenced = cachedFence(text, pos, containers.fence, cr);
      blocks.push({ start: pos, text: text.slice(pos, fenced.end), ...fenced.parsed });
      styled = true;
      pos = fenced.end;
      paragraphStart = pos;
      paragraphOpen = false;
      item = null;
      continue;
    }
    if (BLANK_LINE_RE.test(line)) {
      paragraphLines++;
      pushParagraph(paragraphStart, next);
      paragraphStart = next;
      paragraphOpen = false;
      item = null;
      pos = next;
      continue;
    }
    // A quote starts a paragraph of its own, as does a change of depth; a plain
    // line ends a quote's paragraph unless it's a lazy continuation of it.
    const lazy = !quote && paragraphQuote > 0 && lazyOk && !interruptsParagraph(line);
    // Whether a quote line goes on the open paragraph of a quote at its depth,
    // or (lazily, unless it would interrupt it) of a deeper quote's.
    const continues = quote !== null && paragraphOpen && lazyOk && paragraphQuote === quote.depth;
    const lazyDeeper =
      quote !== null &&
      paragraphOpen &&
      lazyOk &&
      paragraphQuote > quote.depth &&
      !startsListItem(content) &&
      !classifyQuoteLine(line, quote.contentStart, true, null, true).ends;
    const kind: QuoteLineKind | null =
      quote && !lazyDeeper
        ? classifyQuoteLine(
            line,
            quote.contentStart,
            continues,
            item?.depth === quote.depth ? item.column : null,
            false,
          )
        : null;
    if (quote && kind) item = kind.item === null ? null : { depth: quote.depth, column: kind.item };
    else if (!quote && !lazy) item = null;
    const depth = quote ? (lazyDeeper ? paragraphQuote : quote.depth) : lazy ? paragraphQuote : 0;
    if (paragraphOpen && depth !== paragraphQuote) {
      pushParagraph(paragraphStart, pos);
      paragraphStart = pos;
    }
    paragraphLines++;
    paragraphOpen = true;
    paragraphQuote = depth;
    if (quote) lazyOk = true;
    // A quote line that isn't paragraph text (or ends its paragraph as a
    // heading) stands alone, and nothing continues it lazily.
    if (kind?.ends) {
      pushParagraph(paragraphStart, next);
      paragraphStart = next;
      paragraphOpen = false;
      paragraphQuote = 0;
      lazyOk = false;
    }
    pos = next;
  }
  pushParagraph(paragraphStart, text.length);
  return { blocks, styled };
}

/** The draft as unstyled blocks, one per line, for drafts too long to parse. */
export function plainComposerMarkdown(text: string): ComposerMarkdown {
  const blocks: MarkdownBlock[] = [];
  const cr = text.includes("\r");
  for (let line = 0; line < text.length;) {
    const next = nextLineStart(text, lineEndAt(text, line, cr));
    blocks.push({
      start: line,
      text: text.slice(line, next),
      segments: [{ start: 0, end: next - line, flags: 0 }],
      tokens: [],
    });
    line = next;
  }
  return { blocks, styled: false };
}

/**
 * The text a textarea displays for `value`: its value normalizes CRLF and a
 * bare CR to LF. `offset` maps an offset in `value` into the normalized text.
 */
export function normalizeLineBreaks(value: string): {
  text: string;
  offset: (offset: number) => number;
} {
  if (!value.includes("\r")) return { text: value, offset: (offset) => offset };
  const removed: number[] = [];
  for (let i = value.indexOf("\r\n"); i !== -1; i = value.indexOf("\r\n", i + 2)) removed.push(i);
  return {
    text: value.replace(/\r\n?/g, "\n"),
    offset: (offset) => {
      let before = 0;
      while (before < removed.length && removed[before] < offset) before++;
      return offset - before;
    },
  };
}

/** Where the line starting at `pos` ends: its line break, or the end of the text. */
function lineEndAt(text: string, pos: number, cr: boolean): number {
  if (!cr) {
    const end = text.indexOf("\n", pos);
    return end === -1 ? text.length : end;
  }
  for (let i = pos; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code === LF || code === CR) return i;
  }
  return text.length;
}

/** The offset just past the line break at `lineEnd` (LF, CRLF or a bare CR). */
function nextLineStart(text: string, lineEnd: number): number {
  if (lineEnd >= text.length) return lineEnd;
  return text.charCodeAt(lineEnd) === CR && text.charCodeAt(lineEnd + 1) === LF
    ? lineEnd + 2
    : lineEnd + 1;
}

interface FenceOpener {
  containers: FenceContainer[];
  /** The opening line's `>` markers and (new) list markers. */
  marks: RevealRange[];
  items: RevealRange[];
  /** Where its text (the fence, with any indent of its own) starts. */
  contentStart: number;
}

/** A place in a line: its index, column, and the column its container's text starts at. */
interface LineCursor {
  at: number;
  column: number;
  base: number;
}

/**
 * How `line` meets the containers `open` from the line before, as CommonMark
 * reads it: each quote needs its `>` and each list item its indent (or a blank
 * rest), and those that don't go on close, unless the line is lazy paragraph
 * text. Then new quotes and list items (with text on the line) open, and a
 * fence may follow. Returns the containers open after the line, and the fence
 * it opens, if any. Columns count tab stops every 4; a tab after `>` gives one
 * column to the marker and the rest to its text.
 */
function lineContainers(
  line: string,
  open: readonly FenceContainer[],
  paragraphOpen: boolean,
): { open: FenceContainer[]; fence: FenceOpener | null } {
  if (open.length === 0 && !/^[ \t]{0,3}(?:[>`~*+-]|\d)/.test(line))
    return { open: [], fence: null };
  const matched = matchContainers(line, open);
  const all = matched.count === open.length;
  // Lazy paragraph text keeps every container open, and opens none.
  if (!all && paragraphOpen && continuesParagraph(line, matched.cursor))
    return { open: [...open], fence: null };
  const containers = open.slice(0, matched.count);
  const marks = matched.marks;
  const items: RevealRange[] = [];
  let cursor = matched.cursor;
  for (;;) {
    const lead = skipIndent(line, cursor.at, cursor.column);
    if (lead.column - cursor.base > 3) break;
    if (line[lead.at] === ">") {
      marks.push({ start: lead.at, end: lead.at + 1 });
      cursor = afterQuoteMarker(line, lead.at, lead.column);
      containers.push({ quote: true });
      continue;
    }
    const rest = line.slice(lead.at);
    if (THEMATIC_BREAK_RE.test(rest)) break;
    const marker = LIST_MARKER_RE.exec(rest);
    if (!marker) break;
    const markerEnd = lead.at + marker[0].length;
    const markerColumn = lead.column + marker[0].length;
    const text = skipIndent(line, markerEnd, markerColumn);
    // An empty item, or an ordered one not from 1, can't interrupt the open
    // paragraph; past 4 columns after the marker, an item's text is code.
    if (text.at === line.length) break;
    if (paragraphOpen && all && marker[1] !== undefined && marker[1] !== "1") break;
    if (text.column - markerColumn > 4) break;
    items.push({ start: lead.at, end: markerEnd });
    containers.push({ quote: false, indent: text.column - cursor.base });
    cursor = { at: text.at, column: text.column, base: text.column };
  }
  const lead = skipIndent(line, cursor.at, cursor.column);
  const fence =
    lead.column - cursor.base <= 3 && FENCE_OPEN_RE.test(line.slice(lead.at))
      ? { containers, marks, items, contentStart: cursor.at }
      : null;
  return { open: containers, fence };
}

/** Whether a line, past the containers it met, is paragraph text: it starts no block. */
function continuesParagraph(line: string, cursor: LineCursor): boolean {
  const lead = skipIndent(line, cursor.at, cursor.column);
  const rest = line.slice(lead.at);
  if (rest === "") return false;
  if (lead.column - cursor.base >= 4) return true;
  return !(
    rest[0] === ">" ||
    FENCE_OPEN_RE.test(rest) ||
    HEADING_RE.test(rest) ||
    THEMATIC_BREAK_RE.test(rest) ||
    LIST_MARKER_RE.test(rest)
  );
}

/** Past a `>` at `at` (column `column`) and its optional space: where its text starts. */
function afterQuoteMarker(line: string, at: number, column: number): LineCursor {
  const next = at + 1;
  const col = column + 1;
  if (line[next] === " ") return { at: next + 1, column: col + 1, base: col + 1 };
  if (line[next] === "\t") return { at: next + 1, column: col + 4 - (col % 4), base: col + 1 };
  return { at: next, column: col, base: col };
}

/**
 * How many of `containers` a line goes on in, in order: each quote needs its
 * `>`, each list item its indent (or a blank rest of line). With the quote
 * markers it met and where its text resumes after them.
 */
function matchContainers(line: string, containers: readonly FenceContainer[]) {
  const marks: RevealRange[] = [];
  let cursor: LineCursor = { at: 0, column: 0, base: 0 };
  let count = 0;
  for (const container of containers) {
    const lead = skipIndent(line, cursor.at, cursor.column);
    if (container.quote) {
      if (lead.column - cursor.base > 3 || line[lead.at] !== ">") break;
      marks.push({ start: lead.at, end: lead.at + 1 });
      cursor = afterQuoteMarker(line, lead.at, lead.column);
    } else if (lead.at === line.length) {
      cursor = { at: lead.at, column: lead.column, base: lead.column };
    } else {
      if (lead.column - cursor.base < container.indent) break;
      // Take exactly its indent; a tab running past it leaves the rest as text indent.
      let { at, column } = cursor;
      const target = cursor.base + container.indent;
      while (column < target) {
        column = line[at] === "\t" ? column + 4 - (column % 4) : column + 1;
        at++;
      }
      cursor = { at, column, base: target };
    }
    count++;
  }
  return { count, marks, cursor };
}

/**
 * A fenced code block from `blockStart`, opened inside `opener`'s containers:
 * it runs to a closing fence of the same character, at least as long as the
 * opener, or while its lines stay in those containers. Only the fence runs
 * are markers; the info string stays dimmed and visible, and the opening
 * line's list markers plain. An unclosed fence has no fence token, so its
 * opening backticks stay visible (dimmed) like other unclosed markers.
 */
function parseFence(
  text: string,
  blockStart: number,
  opener: FenceOpener,
  cr: boolean,
): { end: number; parsed: ParsedBlock & { fence: MarkdownFence } } {
  const cuts: [number, number, number][] = [];
  const tokens: MarkdownToken[] = [];
  const lines: number[] = [];
  // Quote markers: hidden until edited, one token per line.
  const markQuote = (lineStart: number, marks: readonly RevealRange[], lineEnd: number) => {
    if (marks.length === 0) return;
    const at = marks.map((mark) => ({
      start: lineStart + mark.start - blockStart,
      end: lineStart + mark.end - blockStart,
    }));
    for (const mark of at) cuts.push([mark.start, mark.end, MD_MARKER | MD_QUOTE]);
    tokens.push({ start: lineStart - blockStart, end: lineEnd - blockStart, markers: at });
  };
  const firstEnd = lineEndAt(text, blockStart, cr);
  const firstLine = text.slice(blockStart, firstEnd);
  const openText = skipIndent(firstLine, opener.contentStart, 0).at;
  const openMatch = FENCE_OPEN_RE.exec(firstLine.slice(openText))!;
  const run = openMatch[1];
  const openRun = { start: openText, end: openText + openMatch[0].length };
  markQuote(blockStart, opener.marks, firstEnd);
  for (const item of opener.items) cuts.push([item.start, item.end, 0]);
  lines.push(opener.contentStart);
  let closeRun: RevealRange | null = null;
  let closeLine: number | null = null;
  let pos = nextLineStart(text, firstEnd);
  let end = pos;
  while (pos < text.length) {
    const lineEnd = lineEndAt(text, pos, cr);
    const line = text.slice(pos, lineEnd);
    const prefix = matchContainers(line, opener.containers);
    if (prefix.count < opener.containers.length) break;
    markQuote(pos, prefix.marks, lineEnd);
    lines.push(pos - blockStart + prefix.cursor.at);
    end = nextLineStart(text, lineEnd);
    const lead = skipIndent(line, prefix.cursor.at, prefix.cursor.column);
    const close =
      lead.column - prefix.cursor.base <= 3 ? FENCE_CLOSE_RE.exec(line.slice(lead.at)) : null;
    if (close && close[1][0] === run[0] && close[1].length >= run.length) {
      closeLine = pos - blockStart;
      closeRun = {
        start: pos - blockStart + lead.at,
        end: pos - blockStart + lead.at + close[1].length,
      };
      break;
    }
    pos = end;
  }
  if (pos >= text.length) end = text.length;
  const length = end - blockStart;
  const runFlags = closeRun ? MD_FENCE | MD_MARKER : MD_FENCE | MD_OPEN_FENCE;
  const openLineEnd = firstEnd - blockStart;
  const info = text.slice(blockStart + openRun.end, firstEnd);
  cuts.push(
    [openRun.start, openRun.end, runFlags],
    [openRun.end, openLineEnd, info.trim() ? MD_FENCE | MD_INFO : MD_FENCE],
  );
  if (closeRun) cuts.push([closeRun.start, closeRun.end, runFlags]);
  // Everything else in the block is code.
  const flags = new Uint16Array(length).fill(MD_FENCE);
  const breaks = new Uint8Array(length + 1);
  for (const [start, stop, value] of cuts) {
    flags.fill(value, start, stop);
    breaks[start] = 1;
    breaks[stop] = 1;
  }
  const contentEnd = length - trailingBreakLength(text.slice(blockStart, end));
  if (closeRun) tokens.push({ start: 0, end: contentEnd, markers: [openRun, closeRun] });
  const fence: MarkdownFence = {
    open: closeRun === null,
    run,
    depth: opener.marks.length,
    closeLine,
    prefix: opener.containers
      .map((container) => (container.quote ? "> " : " ".repeat(container.indent)))
      .join(""),
    lines,
  };
  return { end, parsed: { segments: toSegments(flags, breaks), tokens, fence } };
}

function cachedFence(text: string, blockStart: number, opener: FenceOpener, cr: boolean) {
  const fenced = parseFence(text, blockStart, opener, cr);
  // The same lines can be a fence of different containers (a list item opened
  // on an earlier line), which fix its prefix, so they're part of the key.
  const containers = opener.containers
    .map((container) => (container.quote ? ">" : container.indent))
    .join(",");
  const key = `${containers}\u0000${text.slice(blockStart, fenced.end)}`;
  const cached = fenceCache.get(key);
  if (cached) return { end: fenced.end, parsed: cached };
  fenceCache.set(key, fenced.parsed);
  return fenced;
}

function paragraphSegments(paragraph: string): ParsedBlock {
  const cached = paragraphCache.get(paragraph);
  if (cached) return cached;
  const code = findCodePieces(paragraph);
  const quote = quoteLines(paragraph);
  const inline = parseInline(paragraph, code.pieces, quote);
  const tokens = [...code.tokens, ...inline.tokens, ...(quote?.tokens ?? [])];
  const parsed = { segments: inline.segments, tokens };
  paragraphCache.set(paragraph, parsed);
  return parsed;
}

interface QuoteInfo {
  /** Each line's `>` markers. */
  marks: CodePiece[];
  /** Where the quoted text ends: trailing blank lines aren't quoted. */
  end: number;
  /** One token per marked line: the caret anywhere on it reveals its markers. */
  tokens: MarkdownToken[];
}

/**
 * A line's block-quote prefix: up to three spaces, `>` and one optional space,
 * repeated for nested quotes (`> >` or `>>`).
 */
export function quotePrefix(
  line: string,
): { depth: number; marks: RevealRange[]; contentStart: number } | null {
  const marks: RevealRange[] = [];
  let p = 0;
  for (;;) {
    let q = p;
    while (q < line.length && q - p < 3 && line[q] === " ") q++;
    if (line[q] !== ">") break;
    marks.push({ start: q, end: q + 1 });
    p = q + 1;
    if (line[p] === " " || line[p] === "\t") p++;
  }
  return marks.length > 0 ? { depth: marks.length, marks, contentStart: p } : null;
}

interface QuoteLineKind {
  /** The line holds no paragraph text, or ends its paragraph as a heading. */
  ends: boolean;
  /** The column (from the quote's text) of the list item open after it, if any. */
  item: number | null;
}

/**
 * What a quote line holds, for lazy continuation: whether it ends (or isn't)
 * paragraph text, and the list item it leaves open. `continues`: a paragraph at
 * its depth is open, so only what can interrupt one counts, and an underline
 * ends it as a heading. `itemColumn`: an open list item's text column; lines
 * indented that far are its own, and others leave it, unless they go on its
 * paragraph lazily. Indents are in columns, tab stops every 4; a tab after `>`
 * gives one column to the marker and the rest to the text.
 */
function classifyQuoteLine(
  line: string,
  contentStart: number,
  continues: boolean,
  itemColumn: number | null,
  lazyLine: boolean,
): QuoteLineKind {
  let column = 0;
  let tabStart = 0;
  for (let i = 0; i < contentStart; i++) {
    if (line[i] === "\t") {
      tabStart = column;
      column += 4 - (column % 4);
    } else column++;
  }
  const base = line[contentStart - 1] === "\t" ? tabStart + 1 : column;
  const text = skipIndent(line, contentStart, column);
  // A blank line ends a paragraph but not the list item around it.
  if (text.at === line.length) return { ends: true, item: itemColumn };
  const indent = text.column - base;
  if (itemColumn !== null) {
    if (indent >= itemColumn) {
      const inner = blockAt(
        line,
        text.at,
        text.column,
        base + itemColumn,
        base,
        continues,
        lazyLine,
      );
      return { ends: inner.ends, item: inner.item ?? itemColumn };
    }
    const rest = line.slice(text.at);
    // A new item starts beside it; anything else leaves it, unless it goes on
    // the item's paragraph lazily (so an underline there is only text).
    if (indent <= 3 && LIST_MARKER_RE.test(rest))
      return blockAt(line, text.at, text.column, base, base, false, false);
    if (continues) {
      const lazy = blockAt(line, text.at, text.column, base, base, true, true);
      return lazy.ends ? { ends: true, item: lazy.item } : { ends: false, item: itemColumn };
    }
  }
  return blockAt(line, text.at, text.column, base, base, continues, lazyLine);
}

/** What the text at `at` (column `column`, indented from `base`) starts; see `classifyQuoteLine`. */
function blockAt(
  line: string,
  at: number,
  column: number,
  base: number,
  quoteBase: number,
  continues: boolean,
  lazyLine: boolean,
): QuoteLineKind {
  const rest = line.slice(at);
  if (rest === "") return { ends: true, item: null };
  // Four columns in: indented code, or the open paragraph's continuation.
  if (column - base >= 4) return { ends: !continues, item: null };
  if (continues && !lazyLine && SETEXT_UNDERLINE_RE.test(rest)) return { ends: true, item: null };
  if (THEMATIC_BREAK_RE.test(rest) || HEADING_RE.test(rest) || FENCE_OPEN_RE.test(rest))
    return { ends: true, item: null };
  const marker = LIST_MARKER_RE.exec(rest);
  if (!marker) return { ends: false, item: null };
  const markerColumn = column + marker[0].length;
  const content = skipIndent(line, at + marker[0].length, markerColumn);
  const empty = content.at === line.length;
  // An empty item, or an ordered one not starting at 1, can't interrupt a paragraph.
  if (continues && (empty || (marker[1] !== undefined && marker[1] !== "1")))
    return { ends: false, item: null };
  // Past four columns after the marker, the item's text is indented code, and
  // its column (like an empty item's) is one past the marker.
  const code = content.column - markerColumn > 4;
  const itemColumn = (empty || code ? markerColumn + 1 : content.column) - quoteBase;
  if (empty || code) return { ends: true, item: itemColumn };
  const inner = blockAt(line, content.at, content.column, content.column, quoteBase, false, false);
  return { ends: inner.ends, item: inner.item ?? itemColumn };
}

/**
 * Whether a line's text starts a list item. A lazy line that does starts a new
 * block: the rule that an empty or non-1 ordered item can't interrupt a
 * paragraph only holds inside the paragraph's own container.
 */
const startsListItem = (text: string) => LIST_MARKER_RE.test(text.replace(/^ {0,3}/, ""));

/** Past the spaces and tabs from `from` (at `column`): where text resumes, and its column. */
function skipIndent(line: string, from: number, column: number) {
  let at = from;
  let col = column;
  while (at < line.length && (line[at] === " " || line[at] === "\t")) {
    col = line[at] === "\t" ? col + 4 - (col % 4) : col + 1;
    at++;
  }
  return { at, column: col };
}

function interruptsParagraph(line: string): boolean {
  return HEADING_RE.test(line) || THEMATIC_BREAK_RE.test(line) || LIST_ITEM_RE.test(line);
}

/** A quote paragraph's markers and extent, or null for a plain paragraph. */
function quoteLines(paragraph: string): QuoteInfo | null {
  const cr = paragraph.includes("\r");
  if (!quotePrefix(paragraph.slice(0, lineEndAt(paragraph, 0, cr)))) return null;
  const marks: CodePiece[] = [];
  const tokens: MarkdownToken[] = [];
  let end = 0;
  for (let line = 0; line < paragraph.length;) {
    const lineEnd = lineEndAt(paragraph, line, cr);
    const lineText = paragraph.slice(line, lineEnd);
    const next = nextLineStart(paragraph, lineEnd);
    if (!BLANK_LINE_RE.test(lineText)) end = next;
    const prefix = quotePrefix(lineText);
    if (prefix) {
      const lineMarks = prefix.marks.map((mark) => ({
        start: line + mark.start,
        end: line + mark.end,
      }));
      for (const mark of lineMarks) marks.push({ ...mark, flags: MD_MARKER | MD_QUOTE });
      tokens.push({ start: line, end: lineEnd, markers: lineMarks });
    }
    line = next;
  }
  return { marks, end, tokens };
}

function shiftToken(token: MarkdownToken, by: number): MarkdownToken {
  return {
    start: token.start + by,
    end: token.end + by,
    markers: token.markers.map((marker) => ({ start: marker.start + by, end: marker.end + by })),
  };
}

/**
 * One line of a long paragraph, given the paragraph's code pieces (from
 * `first` on), clipped to the line: code resolved across lines stays code.
 */
function lineSegments(
  lineText: string,
  lineStart: number,
  lineEnd: number,
  pieces: readonly CodePiece[],
  first: number,
  quoted: boolean,
): ParsedBlock {
  const clipped: CodePiece[] = [];
  let key = quoted ? `\u0001${lineText}` : lineText;
  for (let i = first; i < pieces.length && pieces[i].start < lineEnd; i++) {
    const start = Math.max(pieces[i].start, lineStart) - lineStart;
    const end = Math.min(pieces[i].end, lineEnd) - lineStart;
    if (end <= start) continue;
    clipped.push({ start, end, flags: pieces[i].flags });
    key += `\u0000${start}:${end}:${pieces[i].flags}`;
  }
  const cached = paragraphCache.get(key);
  if (cached) return cached;
  // Code tokens come from the whole paragraph; the line adds its emphasis.
  const quote = quoted ? lineQuote(lineText) : null;
  const inline = parseInline(lineText, clipped, quote);
  const parsed = {
    segments: inline.segments,
    tokens: [...inline.tokens, ...(quote?.tokens ?? [])],
  };
  paragraphCache.set(key, parsed);
  return parsed;
}

/** One line of a long quote paragraph: quoted unless blank, marked if prefixed. */
function lineQuote(lineText: string): QuoteInfo {
  const prefix = quotePrefix(lineText);
  const marks = (prefix?.marks ?? []).map((mark) => ({ ...mark, flags: MD_MARKER | MD_QUOTE }));
  const contentEnd = lineEndAt(lineText, 0, lineText.includes("\r"));
  return {
    marks,
    end: BLANK_LINE_RE.test(lineText.slice(0, contentEnd)) ? 0 : lineText.length,
    tokens: prefix ? [{ start: 0, end: contentEnd, markers: prefix.marks }] : [],
  };
}

/** A styled stretch of a code span: its opening or closing marker, or its body. */
interface CodePiece {
  start: number;
  end: number;
  flags: number;
}

const CODE_SCAN_RE = /[\\`]/g;

/** Code spans come first in CommonMark: find them before any emphasis. */
function findCodePieces(text: string): { pieces: CodePiece[]; tokens: MarkdownToken[] } {
  const pieces: CodePiece[] = [];
  const tokens: MarkdownToken[] = [];
  const backtickRuns = new BacktickRuns(text);
  CODE_SCAN_RE.lastIndex = 0;
  for (let match = CODE_SCAN_RE.exec(text); match; match = CODE_SCAN_RE.exec(text)) {
    const i = match.index;
    if (text.charCodeAt(i) === BACKSLASH) {
      const escaped = i + 1 < text.length ? text.charCodeAt(i + 1) : 0;
      if (escaped < 128 && ASCII_GROUPS[escaped] === PUNCTUATION) CODE_SCAN_RE.lastIndex = i + 2;
      continue;
    }
    const runEnd = runEndAt(text, i, BACKTICK);
    const close = backtickRuns.closing(runEnd, runEnd - i);
    if (close === -1) {
      CODE_SCAN_RE.lastIndex = runEnd;
      continue;
    }
    const closeEnd = close + (runEnd - i);
    tokens.push({
      start: i,
      end: closeEnd,
      markers: [
        { start: i, end: runEnd },
        { start: close, end: closeEnd },
      ],
    });
    pieces.push(
      { start: i, end: runEnd, flags: MD_CODE | MD_MARKER },
      { start: runEnd, end: close, flags: MD_CODE },
      { start: close, end: closeEnd, flags: MD_CODE | MD_MARKER },
    );
    CODE_SCAN_RE.lastIndex = closeEnd;
  }
  return { pieces: pieces.filter((piece) => piece.end > piece.start), tokens };
}

interface Delimiter {
  char: number;
  /** Unused part of the run; matching consumes it from the inside out. */
  start: number;
  end: number;
  /** The whole run, which reveals every token matched from it. */
  runStart: number;
  runEnd: number;
  /** The link text holding this run, or -1: emphasis never crosses a link's edge. */
  scope: number;
  originalLength: number;
  canOpen: boolean;
  canClose: boolean;
  prev: Delimiter | null;
  next: Delimiter | null;
  index: number;
}

/**
 * Style text given its code pieces: code is never parsed. Links come next, so
 * their brackets and destinations are never read as emphasis; then emphasis and
 * strikethrough. Returns the inline tokens; code tokens come with the pieces.
 */
function parseInline(
  text: string,
  code: readonly CodePiece[],
  quote: QuoteInfo | null = null,
): ParsedBlock {
  const length = text.length;
  const flags = new Uint16Array(length);
  // Segments break at every marker's edges, so adjacent tokens' markers stay apart.
  const breaks = new Uint8Array(length + 1);
  const mark = (piece: CodePiece) => {
    flags.fill(piece.flags, piece.start, piece.end);
    breaks[piece.start] = 1;
    breaks[piece.end] = 1;
  };
  for (const piece of code) mark(piece);
  // A quote's `>` markers come after code: block structure binds tighter.
  const quoteMarks = quote?.marks ?? [];
  for (const piece of quoteMarks) mark(piece);
  const blocking =
    quoteMarks.length > 0 ? [...code, ...quoteMarks].sort((a, b) => a.start - b.start) : code;
  const tokens: MarkdownToken[] = [];
  const links = findLinks(text, blocking, tokens);
  for (const piece of links.pieces) mark(piece);
  // Runs skip code, quote markers and every link marker or destination.
  const skip = [...blocking, ...links.pieces].sort((a, b) => a.start - b.start);
  let head: Delimiter | null = null;
  let tail: Delimiter | null = null;
  let count = 0;
  const tildes: Delimiter[] = [];

  let next = 0;
  let scopeIndex = 0;
  let i = 0;
  while (i < length) {
    if (next < skip.length && i >= skip[next].start) {
      i = Math.max(i, skip[next++].end);
      continue;
    }
    const c = text.charCodeAt(i);
    if (c === BACKSLASH) {
      const escaped = i + 1 < length ? text.charCodeAt(i + 1) : 0;
      i += escaped < 128 && ASCII_GROUPS[escaped] === PUNCTUATION ? 2 : 1;
      continue;
    }
    if (c === STAR || c === UNDERSCORE || c === TILDE) {
      // A run never extends into code or a link marker; the next piece bounds it.
      const limit = next < skip.length ? skip[next].start : length;
      const runEnd = Math.min(runEndAt(text, i, c), limit);
      // GFM strikethrough takes one or two tildes; a longer run is literal.
      if (c === TILDE && runEnd - i > 2) {
        i = runEnd;
        continue;
      }
      while (scopeIndex < links.texts.length && links.texts[scopeIndex].end <= i) scopeIndex++;
      const inLink = scopeIndex < links.texts.length && links.texts[scopeIndex].start <= i;
      const before = i > 0 ? text[i - 1] : "\n";
      const after = runEnd < length ? text[runEnd] : "\n";
      // Tildes flank like `*` (intraword allowed), per the GFM extension.
      const { canOpen, canClose } = flanking(c === TILDE ? STAR : c, before, after);
      if (canOpen || canClose) {
        const delimiter: Delimiter = {
          char: c,
          start: i,
          end: runEnd,
          runStart: i,
          runEnd,
          scope: inLink ? scopeIndex : -1,
          originalLength: runEnd - i,
          canOpen,
          canClose,
          prev: null,
          next: null,
          index: count++,
        };
        if (c === TILDE) tildes.push(delimiter);
        else {
          delimiter.prev = tail;
          if (tail) tail.next = delimiter;
          else head = delimiter;
          tail = delimiter;
        }
      }
      i = runEnd;
      continue;
    }
    i++;
  }

  if (head) processEmphasis(head, flags, breaks, tokens, length);
  if (tildes.length > 0) processStrikethrough(tildes, flags, breaks, tokens, length);
  for (const range of links.texts)
    for (let at = range.start; at < range.end; at++) flags[at] |= MD_LINK;
  if (quote) for (let at = 0; at < quote.end; at++) flags[at] |= MD_QUOTE;
  return { segments: toSegments(flags, breaks), tokens };
}

/**
 * GFM strikethrough: a closing run pairs with the nearest earlier opening run
 * of the same length (one or two tildes) in the same link scope and emphasis
 * context; runs between stay available.
 */
function processStrikethrough(
  tildes: readonly Delimiter[],
  flags: Uint16Array,
  breaks: Uint8Array,
  tokens: MarkdownToken[],
  length: number,
) {
  const depth = new Int32Array(length + 1);
  const openers = new Map<number, Delimiter[]>();
  let matched = false;
  for (const run of tildes) {
    // Like micromark, a pair never crosses an emphasis edge: both runs sit in
    // the same italic and bold context (emphasis is resolved first).
    const context = flags[run.start] & (MD_ITALIC | MD_BOLD);
    const key = ((run.scope + 1) * 4 + context) * 4 + run.originalLength;
    const stack = openers.get(key);
    const opener = run.canClose ? stack?.pop() : undefined;
    if (opener) {
      flags.fill(MD_MARKER, opener.start, opener.end);
      flags.fill(MD_MARKER, run.start, run.end);
      breaks[opener.start] = 1;
      breaks[opener.end] = 1;
      breaks[run.start] = 1;
      breaks[run.end] = 1;
      depth[opener.end]++;
      depth[run.start]--;
      matched = true;
      tokens.push({
        start: opener.start,
        end: run.end,
        markers: [
          { start: opener.start, end: opener.end },
          { start: run.start, end: run.end },
        ],
      });
      continue;
    }
    if (run.canOpen) {
      if (stack) stack.push(run);
      else openers.set(key, [run]);
    }
  }
  if (!matched) return;
  let level = 0;
  for (let at = 0; at < length; at++) {
    level += depth[at];
    if (level > 0) flags[at] |= MD_STRIKE;
  }
}

interface LinkParse {
  /** Markers (`[`, `](`, `)`) and the destination with any title. */
  pieces: CodePiece[];
  /** Each link's text, sorted: the scopes emphasis stays inside. */
  texts: { start: number; end: number }[];
}

/**
 * Inline links, `[text](destination "title")`, per CommonMark: brackets in code
 * or escaped don't count, the innermost of nested links wins, and a
 * destination is a `<...>` or a paren-balanced run with no spaces. Images
 * (`![alt](src)`) and reference links aren't styled.
 */
function findLinks(text: string, code: readonly CodePiece[], tokens: MarkdownToken[]): LinkParse {
  const pieces: CodePiece[] = [];
  const texts: { start: number; end: number }[] = [];
  if (!text.includes("](")) return { pieces, texts };
  const openers: { at: number; image: boolean; active: boolean }[] = [];
  let next = 0;
  let i = 0;
  while (i < text.length) {
    if (next < code.length && i >= code[next].start) {
      i = Math.max(i, code[next++].end);
      continue;
    }
    const c = text.charCodeAt(i);
    if (c === BACKSLASH) {
      i += 2;
      continue;
    }
    if (c === OPEN_BRACKET) {
      const image = i > 0 && text.charCodeAt(i - 1) === BANG && !isEscaped(text, i - 1);
      openers.push({ at: i, image, active: true });
      i++;
      continue;
    }
    if (c !== CLOSE_BRACKET || openers.length === 0) {
      i++;
      continue;
    }
    const opener = openers.pop()!;
    const close = opener.active ? linkDestinationEnd(text, i + 1) : -1;
    // A destination running into code isn't a link here (code binds tighter).
    const codeInside = close !== -1 && code.some((piece) => piece.start < close && piece.end > i);
    if (close === -1 || codeInside) {
      i++;
      continue;
    }
    if (!opener.image) {
      pieces.push(
        { start: opener.at, end: opener.at + 1, flags: MD_MARKER },
        { start: i, end: i + 2, flags: MD_MARKER },
        { start: i + 2, end: close, flags: MD_LINK_URL },
        { start: close, end: close + 1, flags: MD_MARKER },
      );
      texts.push({ start: opener.at + 1, end: i });
      tokens.push({
        start: opener.at,
        end: close + 1,
        markers: [
          { start: opener.at, end: opener.at + 1 },
          { start: i, end: i + 2 },
          { start: close, end: close + 1 },
        ],
      });
      // Links can't contain links: earlier brackets can no longer open one.
      for (const earlier of openers) earlier.active = false;
    }
    i = close + 1;
  }
  texts.sort((a, b) => a.start - b.start);
  return { pieces: pieces.filter((piece) => piece.end > piece.start), texts };
}

function isEscaped(text: string, at: number): boolean {
  let slashes = 0;
  for (let k = at - 1; k >= 0 && text.charCodeAt(k) === BACKSLASH; k--) slashes++;
  return slashes % 2 === 1;
}

/**
 * Parse `(destination "title")` starting at `open` (which must be `(`); return
 * the offset of the closing `)`, or -1 when it isn't a valid inline link tail.
 */
function linkDestinationEnd(text: string, open: number): number {
  if (text.charCodeAt(open) !== OPEN_PAREN) return -1;
  let p = skipLinkSpace(text, open + 1);
  if (p === -1) return -1;
  if (text.charCodeAt(p) === 0x3c) {
    // <destination>: no line breaks or unescaped `<`.
    p++;
    while (p < text.length && text[p] !== ">") {
      if (text[p] === "\n" || text[p] === "<") return -1;
      p += text.charCodeAt(p) === BACKSLASH ? 2 : 1;
    }
    if (p >= text.length) return -1;
    p++;
  } else {
    let depth = 0;
    const start = p;
    while (p < text.length) {
      const c = text.charCodeAt(p);
      if (c === BACKSLASH) {
        p += 2;
        continue;
      }
      if (c <= 0x20) break;
      if (c === OPEN_PAREN) depth++;
      else if (c === CLOSE_PAREN) {
        if (depth === 0) break;
        depth--;
      }
      p++;
    }
    if (depth !== 0) return -1;
    if (p === start && text.charCodeAt(p) !== CLOSE_PAREN) return -1;
  }
  const beforeTitle = p;
  p = skipLinkSpace(text, p);
  if (p === -1) return -1;
  const quote = text[p];
  if (p > beforeTitle && (quote === '"' || quote === "'" || quote === "(")) {
    const closing = quote === "(" ? ")" : quote;
    p++;
    while (p < text.length && text[p] !== closing) {
      if (text.startsWith("\n\n", p)) return -1;
      p += text.charCodeAt(p) === BACKSLASH ? 2 : 1;
    }
    if (p >= text.length) return -1;
    p = skipLinkSpace(text, p + 1);
    if (p === -1) return -1;
  }
  return text.charCodeAt(p) === CLOSE_PAREN ? p : -1;
}

/** Skip spaces and tabs with at most one line break; -1 at a blank line. */
function skipLinkSpace(text: string, from: number): number {
  let p = from;
  let breaks = 0;
  while (p < text.length) {
    const c = text[p];
    if (c === "\n") {
      if (++breaks > 1) return -1;
    } else if (c !== " " && c !== "\t") break;
    p++;
  }
  return p;
}

function runEndAt(text: string, start: number, char: number): number {
  let end = start + 1;
  while (end < text.length && text.charCodeAt(end) === char) end++;
  return end;
}

/**
 * CommonMark's left/right-flanking rules for a delimiter run; `_` additionally
 * refuses to open or close intraword, which keeps snake_case identifiers plain.
 * Neighbors are classified per UTF-16 unit, as micromark (the transcript's
 * parser) does: beside an astral character such as an emoji the neighbor is a
 * surrogate, which counts as a letter, so `🙂_x_🙂` stays plain in both.
 */
function flanking(char: number, before: string, after: string) {
  const beforeGroup = characterGroup(before);
  const afterGroup = characterGroup(after);
  const open = afterGroup === OTHER || (afterGroup === PUNCTUATION && beforeGroup !== OTHER);
  const close = beforeGroup === OTHER || (beforeGroup === PUNCTUATION && afterGroup !== OTHER);
  if (char === STAR) return { canOpen: open, canClose: close };
  return {
    canOpen: open && (beforeGroup !== OTHER || !close),
    canClose: close && (afterGroup !== OTHER || !open),
  };
}

const OTHER = 0;
const WHITESPACE = 1;
const PUNCTUATION = 2;

// ASCII classes precomputed: delimiter neighbors are almost always ASCII.
const ASCII_GROUPS = Uint8Array.from({ length: 128 }, (_, code) => {
  const char = String.fromCharCode(code);
  if (WHITESPACE_RE.test(char)) return WHITESPACE;
  return ASCII_PUNCTUATION_RE.test(char) ? PUNCTUATION : OTHER;
});

function characterGroup(char: string): number {
  const code = char.charCodeAt(0);
  if (code < 128) return ASCII_GROUPS[code];
  if (WHITESPACE_RE.test(char)) return WHITESPACE;
  return PUNCTUATION_RE.test(char) ? PUNCTUATION : OTHER;
}

/** CommonMark's `process_emphasis`, marking styled ranges instead of building nodes. */
function processEmphasis(
  head: Delimiter,
  flags: Uint16Array,
  breaks: Uint8Array,
  tokens: MarkdownToken[],
  length: number,
) {
  // Depth counters as difference arrays: nested emphasis stays linear to apply.
  const italic = new Int32Array(length + 1);
  const bold = new Int32Array(length + 1);
  let matched = false;
  const openersBottom = new Map<number, number>();
  const remove = (delimiter: Delimiter) => {
    if (delimiter.prev) delimiter.prev.next = delimiter.next;
    if (delimiter.next) delimiter.next.prev = delimiter.prev;
  };

  let closer: Delimiter | null = head;
  while (closer) {
    if (!closer.canClose) {
      closer = closer.next;
      continue;
    }
    const key =
      (closer.scope + 1) * 1024 +
      closer.char * 8 +
      (closer.canOpen ? 4 : 0) +
      (closer.originalLength % 3);
    const bottom = openersBottom.get(key) ?? -1;
    let opener: Delimiter | null = closer.prev;
    while (opener && opener.index > bottom) {
      if (opener.char === closer.char && opener.canOpen && opener.scope === closer.scope) {
        const lengths = opener.originalLength + closer.originalLength;
        const oddMatch =
          (closer.canOpen || opener.canClose) &&
          lengths % 3 === 0 &&
          !(opener.originalLength % 3 === 0 && closer.originalLength % 3 === 0);
        if (!oddMatch) break;
      }
      opener = opener.prev;
    }
    if (opener && opener.index > bottom) {
      const use = opener.end - opener.start >= 2 && closer.end - closer.start >= 2 ? 2 : 1;
      flags.fill(MD_MARKER, opener.end - use, opener.end);
      flags.fill(MD_MARKER, closer.start, closer.start + use);
      breaks[opener.end - use] = 1;
      breaks[opener.end] = 1;
      breaks[closer.start] = 1;
      breaks[closer.start + use] = 1;
      tokens.push({
        start: opener.runStart,
        end: closer.runEnd,
        markers: [
          { start: opener.end - use, end: opener.end },
          { start: closer.start, end: closer.start + use },
        ],
      });
      const depth = use === 2 ? bold : italic;
      depth[opener.end]++;
      depth[closer.start]--;
      matched = true;
      opener.end -= use;
      closer.start += use;
      // Delimiters between the pair can no longer match anything.
      opener.next = closer;
      closer.prev = opener;
      if (opener.end === opener.start) remove(opener);
      if (closer.end === closer.start) {
        const next: Delimiter | null = closer.next;
        remove(closer);
        closer = next;
      }
    } else {
      openersBottom.set(key, closer.prev?.index ?? -1);
      const next: Delimiter | null = closer.next;
      if (!closer.canOpen) remove(closer);
      closer = next;
    }
  }

  if (!matched) return;
  let italicDepth = 0;
  let boldDepth = 0;
  for (let i = 0; i < length; i++) {
    italicDepth += italic[i];
    boldDepth += bold[i];
    if (italicDepth > 0) flags[i] |= MD_ITALIC;
    if (boldDepth > 0) flags[i] |= MD_BOLD;
  }
}

/**
 * Backtick runs of a paragraph, indexed by length, for finding a code span's
 * closer. Searches only move forward, so each length's cursor advances
 * monotonically and the whole paragraph stays linear.
 */
class BacktickRuns {
  private readonly text: string;
  private runs: Map<number, number[]> | null = null;
  private cursors = new Map<number, number>();

  constructor(text: string) {
    this.text = text;
  }

  /** Start of the next run of exactly `length` backticks at or after `from`, or -1. */
  closing(from: number, length: number): number {
    const runs = (this.runs ??= this.index()).get(length);
    if (!runs) return -1;
    let cursor = this.cursors.get(length) ?? 0;
    while (cursor < runs.length && runs[cursor] < from) cursor++;
    this.cursors.set(length, cursor);
    return cursor < runs.length ? runs[cursor] : -1;
  }

  private index(): Map<number, number[]> {
    const runs = new Map<number, number[]>();
    let i = this.text.indexOf("`");
    while (i !== -1) {
      const end = runEndAt(this.text, i, BACKTICK);
      const list = runs.get(end - i);
      if (list) list.push(i);
      else runs.set(end - i, [i]);
      i = this.text.indexOf("`", end);
    }
    return runs;
  }
}

function toSegments(flags: Uint16Array, breaks: Uint8Array): MarkdownSegment[] {
  const segments: MarkdownSegment[] = [];
  let start = 0;
  for (let i = 1; i <= flags.length; i++) {
    if (i === flags.length || flags[i] !== flags[start] || breaks[i]) {
      segments.push({ start, end: i, flags: flags[start] });
      start = i;
    }
  }
  return segments;
}

/** A draft range whose Markdown markers show while editing, as offsets into the draft. */
export interface RevealRange {
  start: number;
  end: number;
}

/**
 * The markers a caret or selection reveals, sorted and absolute: those of each
 * token the selection overlaps or touches (right beside either marker counts).
 * A fenced block is touched from anywhere inside it, fence lines included, but
 * not from the line after it. Adjacent tokens reveal separately.
 */
export function markerRevealRanges(
  markdown: ComposerMarkdown,
  selectionStart: number,
  selectionEnd: number,
): RevealRange[] {
  const { blocks } = markdown;
  const ranges: RevealRange[] = [];
  // The last block starting at or before the selection; a token in the block
  // before it can still end right at the caret.
  let low = 0;
  let high = blocks.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (blocks[mid].start <= selectionStart) low = mid;
    else high = mid - 1;
  }
  for (let b = Math.max(0, low - 1); b < blocks.length && blocks[b].start <= selectionEnd; b++) {
    const block = blocks[b];
    for (const token of block.tokens) {
      if (selectionStart > block.start + token.end || selectionEnd < block.start + token.start)
        continue;
      for (const marker of token.markers)
        ranges.push({ start: block.start + marker.start, end: block.start + marker.end });
    }
  }
  ranges.sort((a, b) => a.start - b.start);
  // A code span crossing lines of a long paragraph appears in each line's block.
  return ranges.filter((range, i) => i === 0 || range.start !== ranges[i - 1].start);
}

/** Whether a block is a fenced code block. */
export function isFenceBlock(block: MarkdownBlock): boolean {
  return block.fence !== undefined;
}

/** A fence block with no closing fence. */
export function isOpenFence(block: MarkdownBlock): boolean {
  return block.fence?.open === true;
}

function trailingBreakLength(text: string): number {
  if (text.endsWith("\r\n")) return 2;
  return text.endsWith("\n") || text.endsWith("\r") ? 1 : 0;
}
