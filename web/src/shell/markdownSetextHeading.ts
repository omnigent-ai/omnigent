import type { MarkdownManager } from "@tiptap/markdown";

// Match the interruption rules of marked 17's normal/GFM setext headings.
const INTERRUPT =
  /^(?: {0,3}(?:[*+-]|\d{1,9}[.)]) | {4}| {0,3}\t| {0,3}(?:`{3,}|~{3,}|>|#{1,6})| {0,3}<[^\n>]+>\n)/;
const TABLE = /^ {0,3}\|?(?:[:\- ]*\|)+[:\- ]*\n/;
const UNDERLINE = /^ {0,3}(=+|-+) *$/;
const LINE_TERMINATOR = /[\r\u2028\u2029]/;

export function scanSetextHeading(source: string, gfm: boolean) {
  let end = source.indexOf("\n");
  if (end === -1) return undefined;
  const interrupted = (line: string) => INTERRUPT.test(line) || (gfm && TABLE.test(line));
  if (interrupted(source.slice(0, end + 1))) return undefined;
  if (LINE_TERMINATOR.test(source.slice(0, end))) return undefined;

  while (end < source.length) {
    const start = end + 1;
    const next = source.indexOf("\n", start);
    const lineEnd = next === -1 ? source.length : next;
    const line = source.slice(start, lineEnd);
    const underline = UNDERLINE.exec(line);
    if (end > 0 && underline) {
      let rawEnd = lineEnd;
      while (source[rawEnd] === "\n") rawEnd++;
      return {
        type: "heading" as const,
        raw: source.slice(0, rawEnd),
        depth: underline[1][0] === "=" ? 1 : 2,
        text: source.slice(0, end).trim(),
      };
    }
    if (next === -1 || line.trim() === "" || LINE_TERMINATOR.test(line)) return undefined;
    if (interrupted(source.slice(start, next + 1))) return undefined;
    end = next;
  }
  return undefined;
}

const installed = new WeakSet<MarkdownManager["instance"]>();

// TODO: Remove when marked's setext-heading matcher no longer exhausts the
// regex stack on long lines. Keep the pedantic grammar upstream-owned.
export function installIterativeSetextHeadings(marked: MarkdownManager["instance"]): void {
  if (installed.has(marked)) return;
  marked.use({
    tokenizer: {
      lheading(source) {
        if (this.options.pedantic) return false;
        const heading = scanSetextHeading(source, this.options.gfm !== false);
        return heading ? { ...heading, tokens: this.lexer.inline(heading.text) } : undefined;
      },
    },
  });
  installed.add(marked);
}
