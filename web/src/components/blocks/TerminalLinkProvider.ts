// Clickable http(s) URLs in terminal output. Ported from @xterm/addon-web-links,
// copyright (c) 2017-2019 The xterm.js authors, MIT license (see NOTICE), and
// extended to follow a URL across the hard rows a program leaves at the pane width.

import type { IBufferLine, ILink, ILinkProvider, Terminal } from "@xterm/xterm";

// Everything from http:// or https:// up to the first whitespace or quote,
// excluding RFC 3986/1738 unsafe characters, and ending before trailing
// punctuation or the brackets that usually enclose a URL in prose.
const URL_PATTERN = /(https?|HTTPS?):[/]{2}[^\s"'!*(){}|\\^<>`]*[^\s"':,.!?{}|\\^~[\]`()<>]/;

// Rows joined above and below the hovered row are each capped at this many characters.
const MAX_JOINED_LENGTH = 2048;

export type TerminalLinkActivate = (event: MouseEvent, uri: string) => void;

export class TerminalLinkProvider implements ILinkProvider {
  private readonly term: Terminal;
  private readonly activate: TerminalLinkActivate;

  constructor(term: Terminal, activate: TerminalLinkActivate) {
    this.term = term;
    this.activate = activate;
  }

  provideLinks(bufferLineNumber: number, callback: (links: ILink[] | undefined) => void): void {
    callback(computeLinks(this.term, bufferLineNumber - 1, this.activate));
  }
}

function computeLinks(term: Terminal, lineIndex: number, activate: TerminalLinkActivate): ILink[] {
  const [lines, startLineIndex] = logicalLine(term, lineIndex);
  const text = lines.join("");
  const pattern = new RegExp(URL_PATTERN.source, "g");
  const links: ILink[] = [];
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text))) {
    const uri = match[0];
    if (!isUrl(uri)) continue;
    const [startY, startX] = bufferPosition(term, startLineIndex, 0, match.index);
    if (startY === -1 || startX === -1) continue;
    const [endY, endX] = bufferPosition(term, startY, startX, uri.length);
    if (endY === -1 || endX === -1) continue;
    // Ranges are 1-based with an inclusive end, so only endX keeps its
    // 0-based exclusive value.
    links.push({
      range: { start: { x: startX + 1, y: startY + 1 }, end: { x: endX, y: endY + 1 } },
      text: uri,
      activate,
    });
  }
  return links;
}

/**
 * Rows forming the logical line around ``lineIndex`` and the index of its first row.
 * Expansion stops at whitespace, which ends any URL (a row that trims to nothing
 * counts, so erased wrapped rows never send the scan across the whole scrollback),
 * or at the length cap. Rows are right-trimmed so a URL wrapped early before a wide
 * char still matches; see bufferPosition.
 */
function logicalLine(term: Terminal, lineIndex: number): [string[], number] {
  const buffer = term.buffer.active;
  const current = buffer.getLine(lineIndex);
  if (!current) return [[], lineIndex];
  const currentText = current.translateToString(true);
  const lines = [currentText];
  let topIndex = lineIndex;
  let length = 0;
  if (currentText[0] !== " ") {
    while (length < MAX_JOINED_LENGTH && continuesRowAbove(term, topIndex)) {
      const text = buffer.getLine(topIndex - 1)?.translateToString(true);
      if (!text) break;
      topIndex--;
      length += text.length;
      lines.unshift(text);
      if (text.includes(" ")) break;
    }
  }
  let bottomIndex = lineIndex;
  length = 0;
  while (length < MAX_JOINED_LENGTH && continuesRowAbove(term, bottomIndex + 1)) {
    const text = buffer.getLine(bottomIndex + 1)?.translateToString(true);
    if (!text) break;
    bottomIndex++;
    length += text.length;
    lines.push(text);
    if (text.includes(" ")) break;
  }
  return [lines, topIndex];
}

/** Whether the row at ``rowIndex`` continues the logical line of the row above it. */
function continuesRowAbove(term: Terminal, rowIndex: number): boolean {
  const buffer = term.buffer.active;
  const row = buffer.getLine(rowIndex);
  const above = buffer.getLine(rowIndex - 1);
  if (!row || !above) return false;
  return row.isWrapped || continuesFullRowToken(above, row, term.cols);
}

/**
 * Whether ``row`` holds the tail of a token a width-aware program split at the pane
 * edge: ``above`` is one unbroken token filling every column and ``row`` starts with a
 * non-blank. A row holding prose keeps its hard break, so a URL that merely ends at the
 * edge is never glued to the next word; only a lone token exactly as wide as the pane
 * stays ambiguous.
 */
function continuesFullRowToken(above: IBufferLine, row: IBufferLine, cols: number): boolean {
  return (
    /^\S+$/.test(above.translateToString(false, 0, cols)) && /^\S/.test(row.translateToString(true))
  );
}

/**
 * Map an offset into the joined text back to a 0-based buffer position, walking cells
 * from ``column`` of line ``lineIndex``; ``[-1, -1]`` when the walk leaves the buffer.
 */
function bufferPosition(
  term: Terminal,
  lineIndex: number,
  column: number,
  offset: number,
): [number, number] {
  const buffer = term.buffer.active;
  const cell = buffer.getNullCell();
  let line = lineIndex;
  let start = column;
  let remaining = offset;
  while (remaining) {
    const row = buffer.getLine(line);
    if (!row) return [-1, -1];
    for (let i = start; i < row.length; ++i) {
      row.getCell(i, cell);
      const chars = cell.getChars();
      if (cell.getWidth()) {
        remaining -= chars.length || 1;
        // A wide character that did not fit in the last cell wrapped early,
        // leaving an empty cell that the trimmed row text skipped.
        if (i === row.length - 1 && chars === "") {
          const next = buffer.getLine(line + 1);
          if (next?.isWrapped) {
            next.getCell(0, cell);
            if (cell.getWidth() === 2) remaining += 1;
          }
        }
      }
      if (remaining < 0) return [line, i];
    }
    line++;
    start = 0;
  }
  return [line, start];
}

/** Whether the matched text parses as a URL whose origin is spelled as written. */
function isUrl(text: string): boolean {
  try {
    const url = new URL(text);
    const credentials = url.username
      ? `${url.username}${url.password ? `:${url.password}` : ""}@`
      : "";
    const origin = `${url.protocol}//${credentials}${url.host}`;
    return text.toLowerCase().startsWith(origin.toLowerCase());
  } catch {
    return false;
  }
}
