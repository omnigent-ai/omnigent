// Character offsets into a file's text, as comments store them, and the line
// and column a diff row shows. Lines keep their line endings, as diffs store
// them; a column counts UTF-16 code units within the line's visible text.

export interface LinePosition {
  /** 1-based line number. */
  line: number;
  /** 0-based column within the line, excluding its line ending. */
  column: number;
}

/** One line's part of a character range, in columns of that line's visible text. */
export interface LineSegment {
  line: number;
  from: number;
  to: number;
}

interface IndexedText {
  lines: readonly string[];
  /** Offset at which each line starts. */
  starts: number[];
  length: number;
}

const indexCache = new WeakMap<readonly string[], IndexedText>();

function indexed(lines: readonly string[]): IndexedText {
  const cached = indexCache.get(lines);
  if (cached) return cached;
  const starts: number[] = [];
  let length = 0;
  for (const line of lines) {
    starts.push(length);
    length += line.length;
  }
  const index = { lines, starts, length };
  indexCache.set(lines, index);
  return index;
}

function lineEndingLength(line: string): number {
  if (line.endsWith("\r\n")) return 2;
  return line.endsWith("\n") || line.endsWith("\r") ? 1 : 0;
}

/** Length of a line's visible text. */
export function visibleLength(line: string): number {
  return line.length - lineEndingLength(line);
}

/** The offset of a line and column, clamping the column to the line's visible text. */
export function offsetAt(lines: readonly string[], position: LinePosition): number | undefined {
  const { starts } = indexed(lines);
  const line = lines[position.line - 1];
  if (line === undefined) return undefined;
  const column = Math.max(0, Math.min(position.column, visibleLength(line)));
  return starts[position.line - 1]! + column;
}

/** The line and column of an offset; an offset in a line ending maps to that line's end. */
export function positionAt(lines: readonly string[], offset: number): LinePosition | undefined {
  const { starts, length } = indexed(lines);
  if (lines.length === 0 || offset < 0 || offset > length) return undefined;
  let low = 0;
  let high = lines.length - 1;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (starts[middle]! <= offset) low = middle;
    else high = middle - 1;
  }
  const column = Math.min(offset - starts[low]!, visibleLength(lines[low]!));
  return { line: low + 1, column };
}

/** The visible, non-empty part of `[start, end)` on each line it covers. */
export function lineSegments(lines: readonly string[], start: number, end: number): LineSegment[] {
  const from = positionAt(lines, Math.max(0, start));
  const to = positionAt(lines, Math.min(end, indexed(lines).length));
  if (!from || !to || end <= start) return [];
  const segments: LineSegment[] = [];
  for (let line = from.line; line <= to.line; line += 1) {
    const segment = {
      line,
      from: line === from.line ? from.column : 0,
      to: line === to.line ? to.column : visibleLength(lines[line - 1]!),
    };
    if (segment.to > segment.from) segments.push(segment);
  }
  return segments;
}
