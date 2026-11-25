// A PR description's lines with their exact offsets, so stack metadata can be located
// and spliced without rewriting any other byte, and with the context that keeps a
// format example inside a code fence or a quote from reading as real metadata.

export interface MarkdownLine {
  /** The line without its terminator. */
  text: string;
  /** Offset of the line's first character in the body. */
  start: number;
  /** Offset just past the line's terminator (`\n` or `\r\n`), or the body's end. */
  end: number;
  /** Inside a fenced code block, or a fence line itself. */
  fenced: boolean;
  /** A blockquote line. */
  quoted: boolean;
}

const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const QUOTE = /^ {0,3}>/;

export function markdownLines(body: string): MarkdownLine[] {
  const lines: MarkdownLine[] = [];
  let openFence: string | null = null;
  let start = 0;
  while (start < body.length) {
    const newline = body.indexOf("\n", start);
    const end = newline === -1 ? body.length : newline + 1;
    const raw = body.slice(start, newline === -1 ? end : newline);
    const text = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    const fence = FENCE.exec(text)?.[1] ?? null;
    const fenced = openFence !== null || fence !== null;
    if (openFence === null && fence !== null) {
      openFence = fence;
    } else if (
      openFence !== null &&
      fence !== null &&
      fence[0] === openFence[0] &&
      fence.length >= openFence.length &&
      text.trim() === fence
    ) {
      openFence = null;
    }
    lines.push({ text, start, end, fenced, quoted: !fenced && QUOTE.test(text) });
    start = end;
  }
  return lines;
}
