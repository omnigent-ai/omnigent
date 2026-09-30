/**
 * The composer's compact preview, shown while the draft isn't being edited:
 * the same parse as the live layer, but hidden markers take no space, and a
 * closed code block's fence lines collapse (an info string stays, as a header
 * line). Unclosed markers and fences aren't markers, so they stay visible.
 * The draft itself never changes; the pieces here map compact offsets to
 * draft offsets and back, so a click on the preview puts the caret on the
 * character clicked.
 */
import {
  MD_BOLD,
  MD_CODE,
  MD_FENCE,
  MD_ITALIC,
  MD_LINK,
  MD_LINK_URL,
  MD_MARKER,
  MD_QUOTE,
  MD_STRIKE,
  type ComposerMarkdown,
} from "./composerMarkdown";

/** A visible run of the draft: `text` sits at `compact` in the preview and `source` in the draft. */
export interface CompactPiece {
  compact: number;
  source: number;
  text: string;
  flags: number;
}

/** A draft line the preview shows. */
export interface CompactLine {
  /** Draft offset of the line's start. */
  start: number;
  /** Preview offset of its start, where its text (if any) begins. */
  compact: number;
  pieces: CompactPiece[];
  /** Inside a block quote (its `>` markers may be hidden). */
  quoted: boolean;
}

/** A run of lines: a code block's box, or everything between boxes. */
export interface CompactBlock {
  code: boolean;
  lines: CompactLine[];
}

export interface CompactLayout {
  blocks: CompactBlock[];
  /** Every piece, in order. */
  pieces: CompactPiece[];
  /** The preview's text length. */
  length: number;
  /** The draft's length. */
  sourceLength: number;
  /** Whether anything is hidden; if not, the preview would look like the draft. */
  collapsed: boolean;
}

const pieceEnd = (piece: CompactPiece) => piece.source + piece.text.length;

/** The compact preview of a parsed draft. */
export function compactLayout(markdown: ComposerMarkdown): CompactLayout {
  const blocks: CompactBlock[] = [];
  const pieces: CompactPiece[] = [];
  let compact = 0;
  let collapsed = false;
  let prose: CompactLine[] = [];
  const flushProse = () => {
    if (prose.length > 0) blocks.push({ code: false, lines: prose });
    prose = [];
  };
  for (const block of markdown.blocks) {
    const { fence } = block;
    const fenceLines =
      fence && !fence.open ? [block.start, block.start + (fence.closeLine ?? -1)] : null;
    if (fence) flushProse();
    const lines: CompactLine[] = fence ? [] : prose;
    let line: CompactLine = { start: block.start, compact, pieces: [], quoted: false };
    let hidden = false;
    let visible = "";
    // The optional space after a quote's `>` is part of its marker here.
    let afterQuote = false;
    const endLine = () => {
      // A closed fence's own lines go when only their markers (and indent) show.
      if (fenceLines?.includes(line.start) && hidden && visible.trim() === "") {
        collapsed = true;
        compact -= visible.length;
        pieces.length -= line.pieces.length;
      } else if (line.pieces.length > 0 || hidden) {
        lines.push(line);
      }
    };
    for (const segment of block.segments) {
      let start = segment.start;
      while (start < segment.end) {
        const newline = block.text.indexOf("\n", start);
        const end = newline === -1 || newline >= segment.end ? segment.end : newline + 1;
        if (segment.flags & MD_QUOTE) line.quoted = true;
        if (segment.flags & MD_MARKER) {
          hidden = true;
          collapsed = true;
          // Emphasis markers in a quote carry its flag too; only `>` is its own.
          afterQuote = (segment.flags & MD_QUOTE) !== 0 && block.text[start] === ">";
        } else {
          const from = afterQuote && block.text[start] === " " ? start + 1 : start;
          afterQuote = false;
          if (from < end) {
            const text = block.text.slice(from, end);
            const piece = { compact, source: block.start + from, text, flags: segment.flags };
            line.pieces.push(piece);
            pieces.push(piece);
            compact += text.length;
            visible += text;
          }
        }
        start = end;
        if (end === newline + 1) {
          endLine();
          line = { start: block.start + end, compact, pieces: [], quoted: false };
          hidden = false;
          visible = "";
          afterQuote = false;
        }
      }
    }
    if (line.start < block.start + block.text.length) endLine();
    if (fence && lines.length > 0) blocks.push({ code: true, lines });
  }
  flushProse();
  const sourceLength = markdown.blocks.reduce((sum, block) => sum + block.text.length, 0);
  return { blocks, pieces, length: compact, sourceLength, collapsed };
}

/**
 * How much formatting text typed beside a piece gets: a caret between hidden
 * markers goes to the side that gets less (after a bold word's `**`, before
 * a code span's backtick), a link's destination counting most.
 */
function styleWeight(piece: CompactPiece | undefined): number {
  if (!piece) return 0;
  const { flags } = piece;
  return (
    (flags & MD_ITALIC ? 1 : 0) +
    (flags & MD_BOLD ? 1 : 0) +
    (flags & MD_STRIKE ? 1 : 0) +
    ((flags & (MD_CODE | MD_FENCE)) === MD_CODE ? 1 : 0) +
    (flags & MD_LINK ? 1 : 0) +
    (flags & MD_LINK_URL ? 2 : 0)
  );
}

/** The last piece at or before `offset` by `key`, or -1. */
function pieceAt(pieces: readonly CompactPiece[], offset: number, key: "compact" | "source") {
  let low = 0;
  let high = pieces.length - 1;
  let found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (pieces[mid][key] <= offset) {
      found = mid;
      low = mid + 1;
    } else high = mid - 1;
  }
  return found;
}

/**
 * The draft offset for a preview offset. Inside a piece it's exact; where
 * hidden markers were collapsed it's the side of them that gets less
 * formatting, or their far side on a tie (after a quote's `>`, past a
 * collapsed fence line).
 */
export function compactToSource(layout: CompactLayout, offset: number): number {
  const { pieces } = layout;
  const index = pieceAt(pieces, offset, "compact");
  const piece = pieces[index];
  if (piece && offset > piece.compact && offset < piece.compact + piece.text.length)
    return piece.source + (offset - piece.compact);
  const [left, right] =
    piece && offset === piece.compact ? [pieces[index - 1], piece] : [piece, pieces[index + 1]];
  const from = left ? pieceEnd(left) : 0;
  const to = right ? right.source : layout.sourceLength;
  return styleWeight(left) < styleWeight(right) ? from : to;
}

/** The preview offset for a draft offset; one inside hidden markers lands where they were. */
export function sourceToCompact(layout: CompactLayout, offset: number): number {
  const piece = layout.pieces[pieceAt(layout.pieces, offset, "source")];
  if (!piece) return 0;
  return piece.compact + Math.min(offset - piece.source, piece.text.length);
}
