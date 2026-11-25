// Indentation scopes for sticky scroll: each line's enclosing scope, filled a frame at a time,
// and which scope lines to pin over a view scrolled to a given height. No view code; the
// CodeMirror viewer and the diff views draw the pinned lines themselves.

import type { ChangeSet, Text } from "@codemirror/state";
/** The columns of `text`'s leading whitespace, or null for a blank line. */
export function indentationOf(text: string, tabSize: number): number | null {
  let columns = 0;
  for (const character of text) {
    if (character === " ") columns += 1;
    else if (character === "\t") columns += tabSize - (columns % tabSize);
    else return columns;
  }
  // Blank lines take the indentation of their surroundings.
  return null;
}

/** Monaco's default `stickyScroll.maxLineCount`. */
export const STICKY_SCROLL_MAX_LINES = 5;
/** Pinned lines never take more than this share of the visible lines. */
export const MAX_SHARE_OF_VIEW = 0.5;
/** A pushed-up scope's closing line is looked for only this far below the pinned lines. */
export const CLOSER_SCAN_LINES = 2 * STICKY_SCROLL_MAX_LINES + 2;
const BLANK = -1;
/** Lines whose links one frame fills, so a first jump deep into a huge file can't stall a frame. */
export const FILL_LINES_PER_FRAME = 20_000;

/**
 * Each line's enclosing scope: the nearest line above it that's indented less, so it opens the
 * block the line is in. Only `ScopeFiller` fills links, forward; reads answer from what's filled.
 * An edit drops only the links from its first line on, since a line's scope depends only on the
 * lines above it.
 */
export class IndentationScopes {
  private doc: Text;
  private tabSize: number;
  /** By line number; `BLANK` for a line with only whitespace. Index 0 is unused. */
  private readonly indents: number[] = [0];
  /**
   * By line number; 0 for a line no scope encloses. A blank line holds the nearest non-blank
   * line above it instead (0 if none), whose scope it shares, so no read walks a blank run.
   */
  private readonly parents: number[] = [0];

  constructor(doc: Text, tabSize: number) {
    this.doc = doc;
    this.tabSize = tabSize;
  }

  /** Follows an edit, keeping the links of the lines above its first change. */
  update(doc: Text, tabSize: number, changes: ChangeSet | null): void {
    let keep = this.filledLines;
    if (tabSize !== this.tabSize) keep = 0;
    else if (changes && !changes.empty) {
      let firstChange = Infinity;
      changes.iterChangedRanges((fromA) => {
        firstChange = Math.min(firstChange, fromA);
      });
      keep = Math.min(keep, this.doc.lineAt(firstChange).number - 1);
    }
    this.indents.length = keep + 1;
    this.parents.length = keep + 1;
    this.doc = doc;
    this.tabSize = tabSize;
  }

  /** How many lines, from the first, have their links filled. */
  get filledLines(): number {
    return this.indents.length - 1;
  }

  /** Whether links are filled through `lineNumber`, or through the last line if it's past it. */
  reaches(lineNumber: number): boolean {
    return this.filledLines >= Math.min(lineNumber, this.doc.lines);
  }

  /** The nearest non-blank line at or above a filled `lineNumber`, or 0 if there's none. */
  private nonBlankAt(lineNumber: number): number {
    return this.indents[lineNumber] === BLANK ? this.parents[lineNumber] : lineNumber;
  }

  /** The lines whose scopes enclose `lineNumber`, outermost first; none while it isn't filled. */
  enclosing(lineNumber: number): number[] {
    const chain: number[] = [];
    if (lineNumber < 1 || lineNumber > this.filledLines) return chain;
    const line = this.nonBlankAt(lineNumber);
    if (line < 1) return chain;
    for (let parent = this.parents[line]; parent > 0; parent = this.parents[parent]) {
      chain.push(parent);
    }
    return chain.reverse();
  }

  /** `lineNumber`'s indentation in columns, or null when it's blank or not filled yet. */
  indentation(lineNumber: number): number | null {
    const indent = this.indents[lineNumber];
    return indent === undefined || indent === BLANK ? null : indent;
  }

  /** Fills links toward `lineNumber`, at most `limit` more lines; returns how many it filled. */
  fillToward(lineNumber: number, limit: number): number {
    const filled = this.filledLines;
    const last = Math.min(lineNumber, this.doc.lines, filled + limit);
    if (last <= filled) return 0;
    // The open scopes at the last known line are that line and its chain of parents.
    let nonBlank = filled >= 1 ? this.nonBlankAt(filled) : 0;
    const open: number[] = [];
    for (let line = nonBlank; line > 0; line = this.parents[line]) open.push(line);
    open.reverse();
    let number = filled + 1;
    for (const text of this.doc.iterLines(number, last + 1)) {
      const indent = indentationOf(text, this.tabSize);
      if (indent === null) {
        this.indents[number] = BLANK;
        this.parents[number] = nonBlank;
      } else {
        while (open.length > 0 && this.indents[open[open.length - 1]] >= indent) open.pop();
        this.indents[number] = indent;
        this.parents[number] = open.length > 0 ? open[open.length - 1] : 0;
        open.push(number);
        nonBlank = number;
      }
      number += 1;
    }
    return last - filled;
  }
}

/**
 * Fills scope links for every editor from one animation-frame callback, at most `perFrame`
 * lines per run. Only one run is outstanding at a time, and a run asks for the next only as it
 * ends, so no frame fills more than once; measurements only ask and read.
 */
export class ScopeFiller {
  private readonly perFrame: number;
  private readonly requestFrame: (callback: () => void) => void;
  /** Per scopes, each waiting view's progress callback and the line it waits for. */
  private readonly wanted = new Map<IndentationScopes, Map<() => void, number>>();
  private runRequested = false;

  constructor(perFrame: number, requestFrame: (callback: () => void) => void) {
    this.perFrame = perFrame;
    this.requestFrame = requestFrame;
  }

  /**
   * Asks for links through `lineNumber`; `onProgress` runs after each frame that filled some.
   * Views sharing one scopes each keep their own request, keyed by their callback.
   */
  request(scopes: IndentationScopes, lineNumber: number, onProgress: () => void): void {
    if (scopes.reaches(lineNumber)) return;
    let waiting = this.wanted.get(scopes);
    if (!waiting) {
      waiting = new Map();
      this.wanted.set(scopes, waiting);
    }
    waiting.set(onProgress, lineNumber);
    this.runNextFrame();
  }

  /** Forgets a view's request, as when it closes; without a callback, every request on them. */
  cancel(scopes: IndentationScopes, onProgress?: () => void): void {
    const waiting = this.wanted.get(scopes);
    if (onProgress) waiting?.delete(onProgress);
    if (!onProgress || waiting?.size === 0) this.wanted.delete(scopes);
  }

  private runNextFrame(): void {
    if (this.runRequested) return;
    this.runRequested = true;
    this.requestFrame(() => this.run());
  }

  private run(): void {
    this.runRequested = false;
    let remaining = this.perFrame;
    const progressed: (() => void)[] = [];
    for (const [scopes, waiting] of this.wanted) {
      if (remaining <= 0) break;
      // One fill per scopes, as far as the furthest waiting view needs.
      remaining -= scopes.fillToward(Math.max(...waiting.values()), remaining);
      for (const [onProgress, target] of waiting) {
        if (scopes.reaches(target)) waiting.delete(onProgress);
        progressed.push(onProgress);
      }
      if (waiting.size === 0) this.wanted.delete(scopes);
    }
    if (this.wanted.size > 0) this.runNextFrame();
    for (const onProgress of progressed) onProgress();
  }
}

/** The one filler every sticky-scroll view shares, so together they stay within a frame's lines. */
export const sharedScopeFiller = new ScopeFiller(FILL_LINES_PER_FRAME, (callback) =>
  requestAnimationFrame(callback),
);

/** Where the text is, for working out which lines to pin. Heights are from the document's top. */
export interface StickyScrollGeometry {
  /** The document height at the top of the visible text. */
  top: number;
  lineHeight: number;
  maxLines: number;
  lineCount: number;
  /** The line number at a document height. */
  lineAtHeight: (height: number) => number;
  /** The document height of a line's bottom edge. */
  lineBottom: (lineNumber: number) => number;
}

export interface StickyScrollLines {
  /** Pinned line numbers, outermost first. */
  lines: number[];
  /** How far the innermost pinned line is pushed up as its scope scrolls away, in pixels. */
  pushUp: number;
}

export const NO_STICKY_LINES: StickyScrollLines = { lines: [], pushUp: 0 };

/**
 * The lines to pin over the text. Each pinned row covers the line under it, so the scopes are
 * those of the line under the last row; a scope's own first line isn't pinned while it shows.
 */
export function stickyScrollLines(
  scopes: IndentationScopes,
  geometry: StickyScrollGeometry,
): StickyScrollLines {
  const { top, lineHeight, maxLines } = geometry;
  if (top <= 0 || maxLines <= 0) return NO_STICKY_LINES;
  let lines: number[] = [];
  for (let depth = 0; depth < maxLines; depth += 1) {
    const enclosing = scopes.enclosing(geometry.lineAtHeight(top + depth * lineHeight));
    if (enclosing.length <= depth) break;
    lines = enclosing.slice(0, depth + 1);
  }
  if (lines.length === 0) return NO_STICKY_LINES;
  return { lines, pushUp: innermostPushUp(scopes, geometry, lines) };
}

/** As the innermost scope's last line scrolls under the pinned rows, its row slides up with it. */
function innermostPushUp(
  scopes: IndentationScopes,
  geometry: StickyScrollGeometry,
  lines: number[],
): number {
  const { top, lineHeight, lineCount } = geometry;
  const innermost = lines[lines.length - 1];
  const scopeIndent = scopes.indentation(innermost) ?? 0;
  const rowsBottom = top + lines.length * lineHeight;
  const from = geometry.lineAtHeight(top + (lines.length - 1) * lineHeight);
  const until = Math.min(lineCount, from + CLOSER_SCAN_LINES);
  let lastInside = from;
  for (let line = from + 1; line <= until; line += 1) {
    const indent = scopes.indentation(line);
    if (indent === null) continue;
    if (indent <= scopeIndent) {
      const overlap = rowsBottom - geometry.lineBottom(lastInside);
      return Math.min(Math.max(overlap, 0), lineHeight);
    }
    lastInside = line;
  }
  return 0;
}
