import {
  forwardRef,
  memo,
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { cn } from "@/lib/utils";
import {
  MD_BOLD,
  MD_CODE,
  MD_FENCE,
  MD_ITALIC,
  MD_INFO,
  MD_LINK,
  MD_LINK_URL,
  MD_MARKER,
  MD_OPEN_FENCE,
  MD_QUOTE,
  MD_STRIKE,
  GenerationalCache,
  isFenceBlock,
  isOpenFence,
  type ComposerMarkdown,
  type MarkdownSegment,
  type RevealRange,
} from "@/lib/composerMarkdown";
import {
  compactToSource,
  type CompactLayout,
  type CompactLine,
  type CompactPiece,
} from "@/lib/composerCompact";
import { useItalicSlant } from "./ComposerItalicSlant";

/** Tints a slash command or `$skill` token at the start of the draft. */
const ACCENT = 32;
/** Shows a marker the caret or selection is editing; other markers are hidden. */
const REVEAL = 64;

/** A draft range to tint as a command token, as offsets into the draft. */
export interface ComposerAccentRange {
  start: number;
  end: number;
}

/**
 * Styles here must never move a glyph: the textarea on top owns the caret and
 * selection, so the layer may only change paint (color, background, stroke).
 * Real bold and italic faces change advance widths in the system UI font, so
 * strong text is a faux bold (a thin text stroke) and emphasis is a dotted
 * underline instead of a slanted face: a synthesized oblique needs an
 * `@font-face` alias of the UI font, whose advances differ from the system
 * face's, and skewing inline-block words moves later glyphs by up to 1/64px
 * per word. The underline is a background image inside the span's box: a text
 * decoration's ink can outlive the span's repaint. Hidden markers are
 * transparent, so they keep their width: a hidden `**` leaves a small gap on
 * each side of bold text, and a hidden backtick pads the inline-code pill.
 */
function segmentClass(flags: number): string {
  // A marker's visibility wins over a command token's tint, so a marker inside
  // `/cmd` still hides when not edited.
  const color =
    flags & MD_MARKER
      ? flags & REVEAL
        ? "text-muted-foreground"
        : "text-transparent"
      : flags & ACCENT
        ? "text-brand-accent"
        : flags & (MD_INFO | MD_OPEN_FENCE | MD_LINK_URL)
          ? "text-muted-foreground"
          : flags & (MD_CODE | MD_FENCE)
            ? "text-accent-foreground"
            : flags & MD_LINK
              ? "text-primary"
              : flags & MD_QUOTE
                ? "text-muted-foreground"
                : undefined;
  // Italic dots, strikethrough and link underlines are background layers that
  // compose (see `.md-deco` in index.css), never text-decoration.
  const decorated = flags & (MD_ITALIC | MD_STRIKE | MD_LINK);
  return cn(
    color,
    flags & MD_BOLD && "[-webkit-text-stroke:0.04em_currentColor]",
    decorated && "md-deco",
    flags & MD_ITALIC && "md-em",
    flags & MD_STRIKE && "md-del",
    flags & MD_LINK && "md-a",
  );
}

const classCache = new Map<number, string>();
function classFor(flags: number): string {
  let className = classCache.get(flags);
  if (className === undefined) {
    className = segmentClass(flags);
    classCache.set(flags, className);
  }
  return className;
}

/** Split a line's segments around the accent range and flag the overlap. */
function withAccent(
  segments: readonly MarkdownSegment[],
  lineStart: number,
  accent: ComposerAccentRange,
): MarkdownSegment[] {
  const start = accent.start - lineStart;
  const end = accent.end - lineStart;
  const out: MarkdownSegment[] = [];
  for (const segment of segments) {
    const cuts = [segment.start, Math.max(segment.start, Math.min(start, segment.end))];
    cuts.push(Math.max(cuts[1], Math.min(end, segment.end)), segment.end);
    for (let i = 0; i < 3; i++) {
      if (cuts[i + 1] <= cuts[i]) continue;
      const flags = i === 1 ? segment.flags | ACCENT : segment.flags;
      out.push({ start: cuts[i], end: cuts[i + 1], flags });
    }
  }
  return out;
}

/** Flag the markers inside the reveal ranges; `ranges` is sorted and starts at `first`. */
function withReveal(
  segments: readonly MarkdownSegment[],
  lineStart: number,
  ranges: readonly RevealRange[],
  first: number,
): MarkdownSegment[] {
  return segments.map((segment) => {
    if (!(segment.flags & MD_MARKER)) return segment;
    const start = lineStart + segment.start;
    const end = lineStart + segment.end;
    for (let i = first; i < ranges.length && ranges[i].start < end; i++) {
      if (ranges[i].end > start) return { ...segment, flags: segment.flags | REVEAL };
    }
    return segment;
  });
}

/** One draft line (with its trailing newline) and its styled runs, line-relative. */
export interface HighlightLine {
  text: string;
  segments: MarkdownSegment[];
}

/** Split a block's segments into lines; each line keeps its own `\n`. */
export function splitLines(text: string, segments: readonly MarkdownSegment[]): HighlightLine[] {
  const lines: HighlightLine[] = [];
  let lineStart = 0;
  let current: MarkdownSegment[] = [];
  // Carried forward, so a long line with many segments is scanned once.
  let newline = text.indexOf("\n");
  for (const segment of segments) {
    let start = segment.start;
    while (start < segment.end) {
      if (newline !== -1 && newline < start) newline = text.indexOf("\n", start);
      const end = newline === -1 || newline >= segment.end ? segment.end : newline + 1;
      current.push({ start: start - lineStart, end: end - lineStart, flags: segment.flags });
      start = end;
      if (end === newline + 1) {
        lines.push({ text: text.slice(lineStart, end), segments: current });
        lineStart = end;
        current = [];
      }
    }
  }
  if (lineStart < text.length) lines.push({ text: text.slice(lineStart), segments: current });
  return lines;
}

// Lines are interned by content, so an edit re-renders only the lines whose
// text or styling changed, even inside a re-parsed paragraph; each line's React
// key follows its interned object across edits elsewhere in the draft.
const linesByContent = new GenerationalCache<HighlightLine>(10000);
const linesCache = new WeakMap<readonly MarkdownSegment[], HighlightLine[]>();
const lineIds = new WeakMap<HighlightLine, number>();
let nextLineId = 0;

function internLine(line: HighlightLine): HighlightLine {
  let signature = line.text;
  for (const segment of line.segments) signature += `\u0000${segment.end}:${segment.flags}`;
  const interned = linesByContent.get(signature);
  if (interned) return interned;
  linesByContent.set(signature, line);
  return line;
}

function blockLines(text: string, segments: readonly MarkdownSegment[]): HighlightLine[] {
  let lines = linesCache.get(segments);
  if (!lines) {
    lines = splitLines(text, segments).map(internLine);
    linesCache.set(segments, lines);
  }
  return lines;
}

/** Where a row sits in a run of block-quote rows (see `HighlightLineRow`). */
export type QuotePlace = "only" | "first" | "middle" | "last";

interface Row {
  line: HighlightLine;
  id: number;
  key: string;
  /** The line's draft offset, for placing reveal ranges. */
  start: number;
  /** Its place in a block quote, if the line is quoted. */
  quote?: QuotePlace;
}

const quotedLines = new WeakMap<HighlightLine, boolean>();

/** Whether a line is quoted: `>` lines and a quote's lazy continuations. */
function isQuotedLine(line: HighlightLine): boolean {
  let quoted = quotedLines.get(line);
  if (quoted === undefined) {
    quoted = line.segments.some((segment) => (segment.flags & MD_QUOTE) !== 0);
    quotedLines.set(line, quoted);
  }
  return quoted;
}

/** Mark each run of consecutive quoted rows (`quoted[i]` for `rows[i]`) first to last. */
function placeQuotes(rows: Row[], quoted: boolean[]) {
  for (let i = 0; i < rows.length; i++) {
    if (!quoted[i]) continue;
    const before = i > 0 && quoted[i - 1];
    const after = i + 1 < rows.length && quoted[i + 1];
    rows[i].quote = before ? (after ? "middle" : "last") : after ? "first" : "only";
  }
}

/**
 * `line` is what renders; `base` is its cached, unaccented form, whose id
 * keys the row so revealing markers re-renders it in place.
 */
function toRow(
  line: HighlightLine,
  base: HighlightLine,
  start: number,
  copies: Map<number, number>,
): Row {
  let id = lineIds.get(base);
  if (id === undefined) {
    id = nextLineId++;
    lineIds.set(base, id);
  }
  // Repeated lines (blank lines, a repeated paragraph) share one interned
  // object; number the copies so their keys stay distinct.
  const copy = copies.get(id) ?? 0;
  copies.set(id, copy + 1);
  return { line, id, start, key: copy === 0 ? String(id) : `${id}.${copy}` };
}

/**
 * Group rows into chunks so an edit re-renders and re-lays out one chunk, not
 * every line. Boundaries fall on stable line ids, so an edit elsewhere doesn't
 * reshuffle the chunks around it.
 */
function toChunks(rows: Row[]): Row[][] {
  const chunks: Row[][] = [];
  let chunk: Row[] = [];
  for (const row of rows) {
    const boundary = chunk.length >= 64 || (chunk.length >= 8 && row.id % 32 === 0);
    if (boundary) {
      chunks.push(chunk);
      chunk = [];
    }
    chunk.push(row);
  }
  if (chunk.length > 0) chunks.push(chunk);
  return chunks;
}

function renderSegment(line: HighlightLine, segment: MarkdownSegment): ReactNode {
  const slice = line.text.slice(segment.start, segment.end);
  if (segment.flags === 0) return slice;
  return (
    <span
      key={segment.start}
      className={classFor(segment.flags)}
      data-md={segment.flags & ~REVEAL}
      data-revealed={segment.flags & REVEAL ? "" : undefined}
    >
      {slice}
    </span>
  );
}

const isInlineCode = (segment: MarkdownSegment) =>
  (segment.flags & (MD_CODE | MD_FENCE)) === MD_CODE;

/**
 * One line as its own block. A block per line lets the browser re-lay out only
 * the lines an edit touches; the kept `\n` ends the block without adding a line.
 * An inline code span (backticks included) sits in one tinted pill, so hidden
 * backticks read as its padding.
 */
/** Render counters for tests; nothing sets them outside tests. */
export const highlightLayerRenderHooks: { onRow?: () => void; onChunk?: () => void } = {};

const HighlightLineRow = memo(function HighlightLineRow({
  line,
  quote,
}: {
  line: HighlightLine;
  quote?: QuotePlace;
}) {
  highlightLayerRenderHooks.onRow?.();
  const children: ReactNode[] = [];
  const { segments } = line;
  for (let i = 0; i < segments.length; i++) {
    if (!isInlineCode(segments[i])) {
      children.push(renderSegment(line, segments[i]));
      continue;
    }
    const start = i;
    while (i + 1 < segments.length && isInlineCode(segments[i + 1])) i++;
    children.push(
      <span
        key={`code-${segments[start].start}`}
        data-code-pill=""
        className="rounded-[0.3em] [background-color:var(--code-bg)]"
      >
        {segments.slice(start, i + 1).map((segment) => renderSegment(line, segment))}
      </span>,
    );
  }
  if (!quote) return <div>{children}</div>;
  // A block quote wears the reply quote's look (ReplyDraftBlocks: a 2px
  // primary/60 bar, a muted/40 tint, rounded-md, muted text), drawn by a box
  // behind the row that starts in the layer's left gutter, so no glyph moves.
  // Consecutive quoted rows' boxes stack into one block, rounded at its ends;
  // a row's box covers all its wrapped lines. Unlike the reply quote, the text
  // can't be indented from the bar or padded above and below (that would move
  // glyphs off the textarea's), and the bar sits in the gutter, left of the
  // text column; it's in the same color, width and tint in both themes.
  return (
    <div className="composer-quote relative isolate" data-quote={quote}>
      <span
        aria-hidden
        className={cn(
          "composer-quote-box pointer-events-none absolute inset-y-0 right-0 -left-1.5 -z-10 border-l-2 border-l-primary/60 bg-muted/40",
          (quote === "only" || quote === "first") && "rounded-t-md",
          (quote === "only" || quote === "last") && "rounded-b-md",
        )}
      />
      {children}
    </div>
  );
});

/** The reveal ranges overlapping `[start, end)`; `ranges` is sorted. */
function rangesWithin(ranges: readonly RevealRange[], start: number, end: number) {
  let first = 0;
  while (first < ranges.length && ranges[first].end <= start) first++;
  let last = first;
  while (last < ranges.length && ranges[last].start < end) last++;
  return first === last ? NO_REVEAL : ranges.slice(first, last);
}

const sameRanges = (previous: readonly RevealRange[], next: readonly RevealRange[]) =>
  previous === next ||
  (previous.length === next.length &&
    previous.every((range, i) => range.start === next[i].start && range.end === next[i].end));

const rowsEnd = (rows: Row[]) => {
  const last = rows[rows.length - 1];
  return last.start + last.line.text.length;
};

/**
 * A chunk of lines. Only the rows its reveal ranges touch get new segments, so
 * a caret move re-renders those one or two rows and no others.
 */
const HighlightChunk = memo(
  function HighlightChunk({ rows, reveal }: { rows: Row[]; reveal: readonly RevealRange[] }) {
    highlightLayerRenderHooks.onChunk?.();
    return (
      <div>
        {rows.map((row) => {
          const end = row.start + row.line.text.length;
          const touched = reveal.length > 0 && rangesWithin(reveal, row.start, end) !== NO_REVEAL;
          const line = touched
            ? { text: row.line.text, segments: withReveal(row.line.segments, row.start, reveal, 0) }
            : row.line;
          return <HighlightLineRow key={row.key} line={line} quote={row.quote} />;
        })}
      </div>
    );
  },
  (previous, next) => sameChunk(previous.rows, previous.reveal, next.rows, next.reveal),
);

const sameRows = (previous: Row[], next: Row[]) =>
  previous.length === next.length &&
  previous.every(
    (row, i) => row.line === next[i].line && row.key === next[i].key && row.quote === next[i].quote,
  );

/**
 * Whether a chunk renders the same. Reveal ranges are absolute, so a chunk
 * holding one also needs its rows at the same offsets: rows shifted by an edit
 * above can place equal ranges on different markers. A chunk with nothing
 * revealed doesn't depend on offsets, so an edit above it still skips it.
 */
const sameChunk = (
  previousRows: Row[],
  previousReveal: readonly RevealRange[],
  nextRows: Row[],
  nextReveal: readonly RevealRange[],
) =>
  sameRows(previousRows, nextRows) &&
  sameRanges(previousReveal, nextReveal) &&
  (nextReveal.length === 0 || previousRows.every((row, i) => row.start === nextRows[i].start));

/**
 * A fenced code block as one tinted box: its lines' blocks stack with no gaps
 * and span the full text width, and a wrapper block moves no glyph. The box
 * is bounded by the textarea's text width, so code sits flush with its left
 * edge: inner padding would move glyphs off the textarea's. An unclosed
 * fence's box holds the textarea's empty line after a final newline.
 */
const HighlightCodeBlock = memo(
  function HighlightCodeBlock({
    chunks,
    reveal,
    trailingNewline,
  }: {
    chunks: Row[][];
    reveal: readonly RevealRange[];
    trailingNewline: boolean;
  }) {
    return (
      <div
        data-code-block=""
        data-trailing-newline={trailingNewline ? "" : undefined}
        className="composer-code-block rounded-md [background-color:var(--code-bg)]"
      >
        {chunks.map((rows) => (
          <HighlightChunk
            key={rows[0].key}
            rows={rows}
            reveal={rangesWithin(reveal, rows[0].start, rowsEnd(rows))}
          />
        ))}
      </div>
    );
  },
  (previous, next) =>
    previous.trailingNewline === next.trailingNewline &&
    previous.chunks.length === next.chunks.length &&
    previous.chunks.every((rows, i) =>
      sameChunk(rows, previous.reveal, next.chunks[i], next.reveal),
    ),
);

const NO_REVEAL: readonly RevealRange[] = [];

type LayerItem =
  | { kind: "chunk"; rows: Row[]; start: number; end: number }
  | { kind: "code"; chunks: Row[][]; open: boolean; start: number; end: number };

/**
 * The styled copy of the draft painted behind the (transparent-text) textarea.
 * Its box and typography must match the textarea's exactly; see
 * `ComposerTextarea`, which owns its position, height and scroll. Rows are
 * built once per draft; a caret move only hands reveal ranges to the chunks
 * they fall in.
 */
export const ComposerHighlightLayer = forwardRef<
  HTMLDivElement,
  {
    markdown: ComposerMarkdown;
    accentRange?: ComposerAccentRange | null;
    /** Sorted draft ranges of the markers to show, from `markerRevealRanges`. */
    revealRanges?: readonly RevealRange[];
    disabled?: boolean;
    /** Kept laid out but not painted, while the compact preview shows instead. */
    hidden?: boolean;
  }
>(function ComposerHighlightLayer(
  { markdown, accentRange, revealRanges = NO_REVEAL, disabled, hidden },
  ref,
) {
  const layerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const setLayerRef = useCallback(
    (node: HTMLDivElement | null) => {
      layerRef.current = node;
      if (typeof ref === "function") ref(node);
      else if (ref) ref.current = node;
    },
    [ref],
  );
  useItalicSlant(layerRef, overlayRef, hidden);
  const items = useMemo(() => {
    const copies = new Map<number, number>();
    const built: LayerItem[] = [];
    // Every row in draft order, for joining quoted rows into blocks.
    const ordered: Row[] = [];
    const quoted: boolean[] = [];
    let rows: Row[] = [];
    const flushRows = () => {
      for (const chunk of toChunks(rows))
        built.push({ kind: "chunk", rows: chunk, start: chunk[0].start, end: rowsEnd(chunk) });
      rows = [];
    };
    for (const block of markdown.blocks) {
      const fence = isFenceBlock(block);
      if (fence) flushRows();
      const blockRows: Row[] = fence ? [] : rows;
      let lineStart = block.start;
      for (const cached of blockLines(block.text, block.segments)) {
        const lineEnd = lineStart + cached.text.length;
        const accented = accentRange && accentRange.start < lineEnd && accentRange.end > lineStart;
        const line = accented
          ? { text: cached.text, segments: withAccent(cached.segments, lineStart, accentRange) }
          : cached;
        const row = toRow(line, cached, lineStart, copies);
        blockRows.push(row);
        ordered.push(row);
        quoted.push(isQuotedLine(cached));
        lineStart = lineEnd;
      }
      if (fence && blockRows.length > 0)
        built.push({
          kind: "code",
          chunks: toChunks(blockRows),
          open: isOpenFence(block),
          start: block.start,
          end: lineStart,
        });
    }
    flushRows();
    placeQuotes(ordered, quoted);
    return built;
  }, [markdown, accentRange]);
  const last = markdown.blocks.at(-1);
  const trailingNewline = last?.text.endsWith("\n") ?? false;
  const openFenceAtEnd = last !== undefined && isFenceBlock(last) && isOpenFence(last);
  let first = 0;
  const revealFor = (item: LayerItem) => {
    while (first < revealRanges.length && revealRanges[first].end <= item.start) first++;
    if (first === revealRanges.length || revealRanges[first].start >= item.end) return NO_REVEAL;
    return rangesWithin(revealRanges, item.start, item.end);
  };
  return (
    <div
      ref={setLayerRef}
      aria-hidden
      data-testid="composer-highlight-overlay"
      data-trailing-newline={trailingNewline && !openFenceAtEnd ? "" : undefined}
      className={cn(
        // No scroll anchoring: the textarea is the only scroll authority, so an
        // edit above the viewport must not scroll the layer on its own.
        // It reaches 8px into the input's left gutter, padded back to the
        // textarea's text box, so a block quote's bar can sit left of the text.
        "composer-input-text composer-highlight-layer pointer-events-none absolute -left-2 right-0 top-0 h-full overflow-hidden pl-2 text-ui break-words whitespace-pre-wrap text-foreground select-none [overflow-anchor:none]",
        disabled && "opacity-60",
      )}
      // Inline, not a class: a class change on this root restyles every span.
      style={hidden ? { opacity: 0 } : undefined}
    >
      {items.map((item) =>
        item.kind === "code" ? (
          <HighlightCodeBlock
            key={item.chunks[0][0].key}
            chunks={item.chunks}
            reveal={revealFor(item)}
            trailingNewline={item.open && trailingNewline}
          />
        ) : (
          <HighlightChunk key={item.rows[0].key} rows={item.rows} reveal={revealFor(item)} />
        ),
      )}
      {/* Skewed copies of italic spans (see ComposerItalicSlant); out of the flow. */}
      <div ref={overlayRef} data-italic-overlay="" className="composer-italic-overlay" />
    </div>
  );
});

/**
 * A piece's class in the preview: the layer's, but italic is the real slanted
 * face (nothing needs to line up with the textarea here, so no stand-in).
 */
function compactClassFor(flags: number): string {
  return flags & MD_ITALIC ? cn(classFor(flags & ~MD_ITALIC), "italic") : classFor(flags);
}

/** A piece: plain text as a bare text node, styled text in a span per accent cut. */
function renderPiece(piece: CompactPiece, accent: ComposerAccentRange | null): ReactNode[] {
  const end = piece.source + piece.text.length;
  const cuts = [piece.source, end];
  if (accent && accent.start < end && accent.end > piece.source)
    cuts.splice(1, 0, Math.max(piece.source, accent.start), Math.min(end, accent.end));
  const nodes: ReactNode[] = [];
  for (let i = 0; i + 1 < cuts.length; i++) {
    if (cuts[i + 1] <= cuts[i]) continue;
    const accented = accent !== null && cuts[i] >= accent.start && cuts[i + 1] <= accent.end;
    const flags = accented ? piece.flags | ACCENT : piece.flags;
    const at = cuts[i] - piece.source;
    const text = piece.text.slice(at, cuts[i + 1] - piece.source);
    if (flags === 0) {
      nodes.push(text);
      continue;
    }
    nodes.push(
      <span
        key={at}
        data-md={flags}
        className={cn(
          compactClassFor(flags),
          // A link's destination follows its text with no brackets between.
          flags & MD_LINK_URL && at === 0 && "ms-[0.3em]",
        )}
      >
        {text}
      </span>,
    );
  }
  return nodes;
}

const isInlineCodePiece = (piece: CompactPiece) => (piece.flags & (MD_CODE | MD_FENCE)) === MD_CODE;

const CompactRow = memo(function CompactRow({
  line,
  quote,
  accent,
}: {
  line: CompactLine;
  quote?: QuotePlace;
  accent: ComposerAccentRange | null;
}) {
  const children: ReactNode[] = [];
  const { pieces } = line;
  for (let i = 0; i < pieces.length; i++) {
    if (!isInlineCodePiece(pieces[i])) {
      children.push(...renderPiece(pieces[i], accent));
      continue;
    }
    // Inline code keeps its pill; with no backticks to pad it, it pads itself.
    const start = i;
    while (i + 1 < pieces.length && isInlineCodePiece(pieces[i + 1])) i++;
    children.push(
      <span
        key={`code-${pieces[start].source}`}
        data-code-pill=""
        className="rounded-[0.3em] px-[0.2em] [background-color:var(--code-bg)]"
      >
        {pieces.slice(start, i + 1).flatMap((piece) => renderPiece(piece, accent))}
      </span>,
    );
  }
  if (!quote) return <div data-row={line.compact}>{children}</div>;
  return (
    <div className="composer-quote relative isolate" data-quote={quote} data-row={line.compact}>
      <span
        aria-hidden
        className={cn(
          "composer-quote-box pointer-events-none absolute inset-y-0 right-0 -left-1.5 -z-10 border-l-2 border-l-primary/60 bg-muted/40",
          (quote === "only" || quote === "first") && "rounded-t-md",
          (quote === "only" || quote === "last") && "rounded-b-md",
        )}
      />
      {children}
    </div>
  );
});

/**
 * The preview offset of a DOM caret position inside the compact view: its
 * row's start plus the text before it in the row (a quote's box has none).
 */
export function compactOffsetAt(root: HTMLElement, node: Node, offset: number): number | null {
  const element = node instanceof Element ? node : node.parentElement;
  const row = element?.closest<HTMLElement>("[data-row]");
  if (!row || !root.contains(row)) {
    // Between rows or off them: the nearest row's edge.
    if (!(node instanceof Element) || !root.contains(node)) return null;
    const rows = node.querySelectorAll<HTMLElement>("[data-row]");
    const next = node.childNodes[offset];
    const after = Array.from(rows).find((candidate) =>
      next ? next === candidate || next.contains(candidate) || isAfter(candidate, next) : false,
    );
    if (after) return Number(after.dataset.row);
    const last = rows[rows.length - 1];
    return last ? Number(last.dataset.row) + textLength(last) : null;
  }
  // The text node at (or right after) the caret position, and how far into it.
  let target: Node | null = node;
  let into = offset;
  if (node instanceof Element) {
    target = node.childNodes[offset] ?? null;
    into = 0;
    if (target === null) return Number(row.dataset.row) + textUpTo(row, node, true);
  }
  return Number(row.dataset.row) + textUpTo(row, target, false) + into;
}

/** Whether `node` follows `reference` in document order. */
const isAfter = (node: Node, reference: Node) =>
  (reference.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;

/** The row's text length, a quote's box aside (it holds none). */
function textLength(row: HTMLElement): number {
  return textUpTo(row, null, false);
}

/**
 * The length of the row's text before `stop` (or through all of `stop` when
 * `through`, or the whole row when `stop` is null).
 */
function textUpTo(row: HTMLElement, stop: Node | null, through: boolean): number {
  const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
  let length = 0;
  for (let text = walker.nextNode(); text; text = walker.nextNode()) {
    if (stop && !through && (text === stop || isAfter(text, stop))) break;
    if (stop && through && !stop.contains(text) && isAfter(text, stop)) break;
    length += (text as Text).length;
  }
  return length;
}

/** The DOM caret position under a point, where the browser can tell. */
function caretPointAt(x: number, y: number): { node: Node; offset: number } | null {
  const doc = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
    caretRangeFromPoint?: (x: number, y: number) => Range | null;
  };
  const position = doc.caretPositionFromPoint?.(x, y);
  if (position) return { node: position.offsetNode, offset: position.offset };
  const range = doc.caretRangeFromPoint?.(x, y);
  return range ? { node: range.startContainer, offset: range.startOffset } : null;
}

/** Lines per chunk of the preview; only chunks near its scroll position are rendered. */
const COMPACT_CHUNK_LINES = 64;

interface CompactChunkRows {
  /** Stable across renders of the same draft: its first line's draft offset. */
  key: number;
  /** Its first line's preview offset, for a click on its placeholder. */
  compact: number;
  rows: { line: CompactLine; quote?: QuotePlace }[];
}

/** Which chunks (by index across the preview) are rendered. */
interface ChunkRange {
  first: number;
  last: number;
}

/**
 * The chunks around a scroll position, one view height either side. Heights
 * are measured once rendered, estimated from the line height until then.
 */
function chunksAround(heights: readonly number[], top: number, viewHeight: number): ChunkRange {
  let offset = 0;
  let first = -1;
  let last = heights.length - 1;
  for (let i = 0; i < heights.length; i++) {
    const end = offset + heights[i];
    if (first === -1 && end >= top - viewHeight) first = i;
    if (offset > top + viewHeight * 2) {
      last = i - 1;
      break;
    }
    offset = end;
  }
  return { first: Math.max(0, first), last: Math.max(Math.max(0, first), last) };
}

/**
 * The draft's compact preview, shown in the textarea's place while it isn't
 * focused: hidden markers take no space and a closed code block's fence
 * lines collapse. It's aria-hidden; the textarea stays the input. A click or
 * tap puts the caret on the draft character under it (`onPlaceCaret`), and
 * the textarea's own layout comes back. It's never taller than the textarea
 * (it scrolls instead), and opens scrolled to the same place. A long draft
 * renders only the chunks of lines near its scroll position, so opening and
 * closing it stays quick; the rest are placeholders of their height.
 */
export const ComposerCompactView = memo(function ComposerCompactView({
  layout,
  accentRange = null,
  disabled,
  textareaRef,
  onPlaceCaret,
}: {
  layout: CompactLayout;
  accentRange?: ComposerAccentRange | null;
  disabled?: boolean;
  /** The textarea it stands in for: its height caps the preview, its scroll places it. */
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  onPlaceCaret: (source: number) => void;
}) {
  const viewRef = useRef<HTMLDivElement>(null);
  const pointerTypeRef = useRef("mouse");
  const blocks = useMemo(() => {
    const lines = layout.blocks.flatMap((block) => block.lines);
    const quotes: (QuotePlace | undefined)[] = lines.map((line, i) => {
      if (!line.quoted) return undefined;
      const before = i > 0 && lines[i - 1].quoted;
      const after = i + 1 < lines.length && lines[i + 1].quoted;
      return before ? (after ? "middle" : "last") : after ? "first" : "only";
    });
    let index = 0;
    return layout.blocks.map((block) => {
      const rows = block.lines.map((line) => ({ line, quote: quotes[index++] }));
      const chunks: CompactChunkRows[] = [];
      for (let i = 0; i < rows.length; i += COMPACT_CHUNK_LINES) {
        const slice = rows.slice(i, i + COMPACT_CHUNK_LINES);
        chunks.push({ key: slice[0].line.start, compact: slice[0].line.compact, rows: slice });
      }
      return { code: block.code, key: block.lines[0]?.start ?? 0, chunks };
    });
  }, [layout]);
  const chunks = useMemo(() => blocks.flatMap((block) => block.chunks), [blocks]);
  // Rendered chunks' measured heights, by key; the rest are estimated.
  const measuredRef = useRef(new Map<number, number>());
  const lineHeightRef = useRef(0);
  const heights = () =>
    chunks.map(
      (chunk) => measuredRef.current.get(chunk.key) ?? chunk.rows.length * lineHeightRef.current,
    );
  const [range, setRange] = useState<ChunkRange | null>(null);
  // Before the first layout: everything for a short draft, else the end or the
  // start (whichever the textarea is nearer), refined once measured.
  const shown =
    range ??
    (chunks.length <= 3 || !textareaRef.current
      ? { first: 0, last: chunks.length - 1 }
      : textareaRef.current.scrollTop > textareaRef.current.scrollHeight / 2
        ? { first: Math.max(0, chunks.length - 3), last: chunks.length - 1 }
        : { first: 0, last: 2 });
  const measure = () => {
    const view = viewRef.current;
    if (!view) return;
    for (const element of view.querySelectorAll<HTMLElement>("[data-chunk]"))
      measuredRef.current.set(Number(element.dataset.chunk), element.offsetHeight);
  };
  const update = () => {
    const view = viewRef.current;
    if (!view || chunks.length <= 3) return;
    measure();
    const next = chunksAround(heights(), view.scrollTop, view.clientHeight);
    setRange((previous) =>
      previous?.first === next.first && previous.last === next.last ? previous : next,
    );
  };
  useLayoutEffect(() => {
    const view = viewRef.current;
    const textarea = textareaRef.current;
    if (!view || !textarea) return;
    lineHeightRef.current = parseFloat(getComputedStyle(textarea).lineHeight) || 20;
    const fit = () => {
      const height = textarea.getBoundingClientRect().height;
      if (height > 0) view.style.maxHeight = `${height}px`;
    };
    fit();
    // Open at the textarea's scroll position, in proportion.
    const scrollable = textarea.scrollHeight - textarea.clientHeight;
    const fraction = scrollable > 0 ? textarea.scrollTop / scrollable : 0;
    measure();
    view.scrollTop = fraction * (view.scrollHeight - view.clientHeight);
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(fit);
    observer.observe(textarea);
    return () => observer.disconnect();
    // Mount only: the draft's scroll position is read once, when it opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [textareaRef]);
  // A draft that changes while shown (dictation) may move the chunks in view.
  const mountedRef = useRef(false);
  useLayoutEffect(() => {
    if (mountedRef.current) update();
    mountedRef.current = true;
    // `update` reads the current chunks; it changes with them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chunks]);
  const place = (x: number, y: number) => {
    const view = viewRef.current;
    if (!view || disabled) return;
    const point = caretPointAt(x, y);
    const at = point ? compactOffsetAt(view, point.node, point.offset) : null;
    onPlaceCaret(compactToSource(layout, at ?? layout.length));
  };
  let index = 0;
  const renderChunk = (chunk: CompactChunkRows) => {
    const at = index++;
    if (at < shown.first || at > shown.last) {
      const height =
        measuredRef.current.get(chunk.key) ?? chunk.rows.length * lineHeightRef.current;
      return (
        <div
          key={chunk.key}
          data-row={chunk.compact}
          data-placeholder=""
          style={{ height: `${height}px` }}
        />
      );
    }
    return (
      <div key={chunk.key} data-chunk={chunk.key}>
        {chunk.rows.map(({ line, quote }) => (
          <CompactRow key={line.start} line={line} quote={quote} accent={accentRange} />
        ))}
      </div>
    );
  };
  return (
    <div
      ref={viewRef}
      aria-hidden
      data-testid="composer-compact-view"
      className={cn(
        // Its own 8px left gutter, like the layer's, for a quote's bar.
        "composer-input-text composer-highlight-layer relative -ml-2 cursor-text overflow-y-auto pl-2 text-ui break-words whitespace-pre-wrap text-foreground select-none [overflow-anchor:none] [scrollbar-width:none] md:min-h-[42px] [&::-webkit-scrollbar]:hidden",
        disabled && "cursor-default opacity-60",
      )}
      onScroll={update}
      onPointerDown={(event) => {
        pointerTypeRef.current = event.pointerType;
      }}
      onMouseDown={(event) => {
        // A mouse press places the caret at once; cancelling it keeps focus
        // off the page. A touch waits for its tap, so a drag still scrolls.
        if (pointerTypeRef.current !== "mouse" || event.button !== 0) return;
        event.preventDefault();
        place(event.clientX, event.clientY);
      }}
      onClick={(event) => {
        if (pointerTypeRef.current === "mouse") return;
        place(event.clientX, event.clientY);
      }}
    >
      {blocks.map(({ code, key, chunks: blockChunks }) => {
        const content = blockChunks.map(renderChunk);
        return code ? (
          <div
            key={key}
            data-code-block=""
            className="composer-code-block rounded-md px-1.5 py-1 [background-color:var(--code-bg)]"
          >
            {content}
          </div>
        ) : (
          <div key={key}>{content}</div>
        );
      })}
    </div>
  );
});
