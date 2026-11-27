// A PR description's lines with their exact offsets, so stack metadata can be located
// and spliced without rewriting any other byte, and with the context that keeps a
// format example inside code, raw HTML or a quote from reading as real metadata.

import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";

export interface MarkdownLine {
  /** The line without its terminator. */
  text: string;
  /** Offset of the line's first character in the body. */
  start: number;
  /** Offset just past the line's terminator (`\n` or `\r\n`), or the body's end. */
  end: number;
  /** Inside a code block or a block of raw HTML, fence lines included. */
  fenced: boolean;
  /** Inside a blockquote, and not in code. */
  quoted: boolean;
}

/** The parts of a parsed tree this reads. */
interface MarkdownNode {
  type: string;
  position?: { start: { offset?: number }; end: { offset?: number } };
  children?: MarkdownNode[];
}

interface Range {
  start: number;
  end: number;
}

const parser = unified().use(remarkParse).use(remarkGfm);
// Containers whose children are blocks, so raw HTML directly inside them is an HTML block.
const BLOCK_CONTAINERS = new Set(["root", "blockquote", "listItem", "footnoteDefinition"]);

/** Where Markdown itself puts code and raw HTML blocks, and quotes, by offset in `body`. */
function literalRanges(body: string): { code: Range[]; quotes: Range[] } {
  const code: Range[] = [];
  const quotes: Range[] = [];
  const visit = (node: MarkdownNode, parent: string | null) => {
    const start = node.position?.start.offset;
    const end = node.position?.end.offset;
    const placed = start !== undefined && end !== undefined;
    const htmlBlock = node.type === "html" && parent !== null && BLOCK_CONTAINERS.has(parent);
    if (placed && (node.type === "code" || htmlBlock)) {
      code.push({ start, end });
      return;
    }
    if (placed && node.type === "blockquote") quotes.push({ start, end });
    for (const child of node.children ?? []) visit(child, node.type);
  };
  visit(parser.parse(body) as MarkdownNode, null);
  return { code, quotes };
}

// A blank line counts by its start, so one just past a block's last character stays outside it.
const within = (ranges: readonly Range[], start: number, length: number) =>
  ranges.some((range) => start < range.end && range.start < start + Math.max(length, 1));

export function markdownLines(body: string): MarkdownLine[] {
  const { code, quotes } = literalRanges(body);
  const lines: MarkdownLine[] = [];
  let start = 0;
  while (start < body.length) {
    const newline = body.indexOf("\n", start);
    const end = newline === -1 ? body.length : newline + 1;
    const raw = body.slice(start, newline === -1 ? end : newline);
    const text = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    const fenced = within(code, start, text.length);
    lines.push({ text, start, end, fenced, quoted: !fenced && within(quotes, start, text.length) });
    start = end;
  }
  return lines;
}
