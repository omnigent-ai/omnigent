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
// Markdown lets only spaces and tabs follow a closing fence; `trim()` would also take U+00A0.
const CLOSING_FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;
const QUOTE = /^ {0,3}>/;
const LIST_ITEM = /^ {0,3}([-*+]|\d{1,9}[.)])( |\t|$)/;

const indentOf = (text: string): number => text.length - text.trimStart().length;

/** An open code fence: its marker, and the list item's indent when it opened inside one. */
interface OpenFence {
  marker: string;
  itemIndent: number | null;
}

export function markdownLines(body: string): MarkdownLine[] {
  const lines: MarkdownLine[] = [];
  let open: OpenFence | null = null;
  // Whether the lines read so far sit in a list item, so an indented fence belongs to it.
  let inListItem = false;
  let start = 0;
  while (start < body.length) {
    const newline = body.indexOf("\n", start);
    const end = newline === -1 ? body.length : newline + 1;
    const raw = body.slice(start, newline === -1 ? end : newline);
    const text = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    const blank = text.trim() === "";
    // A line indented less than its list item ends the item, and a code fence inside it.
    if (open?.itemIndent != null && !blank && indentOf(text) < open.itemIndent) open = null;
    if (open !== null) {
      const content = open.itemIndent === null ? text : text.slice(open.itemIndent);
      const closing = CLOSING_FENCE.exec(content)?.[1];
      if (closing && closing[0] === open.marker[0] && closing.length >= open.marker.length) {
        open = null;
      }
      lines.push({ text, start, end, fenced: true, quoted: false });
      start = end;
      continue;
    }
    const content = text.trimStart();
    const fence = FENCE.exec(inListItem ? content : text)?.[1] ?? null;
    // A backtick fence's info string holds no backtick, so "```a```" is inline code, not a fence.
    const opens =
      fence !== null &&
      !(fence[0] === "`" && text.slice(text.indexOf(fence) + fence.length).includes("`"));
    if (opens && fence !== null) {
      const indent = indentOf(text);
      open = { marker: fence, itemIndent: inListItem && indent > 0 ? indent : null };
    } else if (!blank) {
      if (LIST_ITEM.test(text)) inListItem = true;
      else if (indentOf(text) === 0) inListItem = false;
    }
    lines.push({ text, start, end, fenced: opens, quoted: !opens && QUOTE.test(text) });
    start = end;
  }
  return lines;
}
