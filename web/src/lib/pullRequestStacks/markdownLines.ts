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
// A list item's marker and the spaces after it, which set where its content starts.
const LIST_ITEM = /^( {0,3})([-*+]|\d{1,9}[.)])([ \t]+|$)/;

const indentOf = (text: string): number => text.length - text.trimStart().length;

/** Where a list item's content starts, or null for a line that opens no item. */
function listContentIndent(text: string): number | null {
  const item = LIST_ITEM.exec(text);
  if (!item) return null;
  const spaces = item[3]?.length ?? 0;
  // Five or more spaces after the marker start indented code: the content starts one past it.
  return item[1]!.length + item[2]!.length + (spaces >= 1 && spaces <= 4 ? spaces : 1);
}

/** An open code fence: its marker, and its list item's content indent when it opened in one. */
interface OpenFence {
  marker: string;
  itemIndent: number | null;
}

export function markdownLines(body: string): MarkdownLine[] {
  const lines: MarkdownLine[] = [];
  let open: OpenFence | null = null;
  // Where the current list item's content starts, so a fence indented that far belongs to it.
  let itemIndent: number | null = null;
  let start = 0;
  while (start < body.length) {
    const newline = body.indexOf("\n", start);
    const end = newline === -1 ? body.length : newline + 1;
    const raw = body.slice(start, newline === -1 ? end : newline);
    const text = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    const blank = text.trim() === "";
    const indent = indentOf(text);
    // A line indented less than its list item's content ends the item, and a fence inside it.
    if (itemIndent !== null && !blank && indent < itemIndent) {
      if (open?.itemIndent != null) open = null;
      itemIndent = null;
    }
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
    const content = itemIndent === null ? text : text.slice(itemIndent);
    const fence = FENCE.exec(content)?.[1] ?? null;
    // A backtick fence's info string holds no backtick, so "```a```" is inline code, not a fence.
    const opens =
      fence !== null &&
      !(fence[0] === "`" && text.slice(text.indexOf(fence) + fence.length).includes("`"));
    if (opens && fence !== null) {
      open = { marker: fence, itemIndent };
    } else if (!blank) {
      itemIndent = listContentIndent(text) ?? itemIndent;
    }
    lines.push({ text, start, end, fenced: opens, quoted: !opens && QUOTE.test(text) });
    start = end;
  }
  return lines;
}
