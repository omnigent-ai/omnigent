// Marks code blocks whose fence never closed, so a chart draws only after its
// closing fence. Streamdown reports an open fence only while text streams.

interface HastNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
  position?: { start: { offset?: number }; end: { offset?: number } };
}

/** Prop a code block's `pre` receives when its fence never closed. */
export const UNCLOSED_FENCE_PROP = "data-unclosed-fence";

const OPENING_FENCE = /^([ \t]*)(`{3,}|~{3,})/;
const CLOSING_FENCE = /^(`{3,}|~{3,})[ \t]*$/;
const QUOTE_MARKER = /^[ \t]{0,3}>[ \t]?/;

/** Columns *text* spans, with tabs to the next multiple of 4. */
function columnWidth(text: string): number {
  let width = 0;
  for (const char of text) width = char === "\t" ? width + 4 - (width % 4) : width + 1;
  return width;
}

/** *line* without *depth* blockquote markers, or null when it has fewer. */
function withoutQuoteMarkers(line: string, depth: number): string | null {
  let rest = line;
  for (let level = 0; level < depth; level += 1) {
    const marker = rest.match(QUOTE_MARKER)?.[0];
    if (marker === undefined) return null;
    rest = rest.slice(marker.length);
  }
  return rest;
}

/**
 * Whether a fenced code block's source (from its opening fence to its last line)
 * ends with a closing fence: the opening character, at least as many times,
 * indented at most 3 columns past the block's container. Indented code has no
 * fence and counts as closed.
 *
 * :param blockSource: The block's Markdown, e.g. ``"```vega-lite\n{}\n```"``.
 * :param linePrefix: Text before the opening fence on its line, such as a list
 *   marker (``"1. "``) or blockquote marker (``"> "``).
 */
export function isFenceClosed(blockSource: string, linePrefix = ""): boolean {
  const lines = blockSource.trimEnd().split(/\r?\n/);
  const opening = lines[0].match(OPENING_FENCE);
  if (!opening) return true;
  if (lines.length < 2) return false;
  const quoteDepth = linePrefix.split(">").length - 1;
  const container = withoutQuoteMarkers(linePrefix, quoteDepth) ?? "";
  const maxIndent = columnWidth(container) + columnWidth(opening[1]) + 3;
  const lastLine = withoutQuoteMarkers(lines[lines.length - 1], quoteDepth);
  if (lastLine === null) return false;
  const indent = lastLine.match(/^[ \t]*/)![0];
  if (columnWidth(indent) > maxIndent) return false;
  const closer = lastLine.slice(indent.length).match(CLOSING_FENCE)?.[1];
  const opener = opening[2];
  return closer !== undefined && closer[0] === opener[0] && closer.length >= opener.length;
}

/**
 * Rehype plugin that sets {@link UNCLOSED_FENCE_PROP} on each `pre` whose fence
 * never closed, judged from the Markdown source the parser saw. Works whether
 * or not the text is streaming, and inside lists and blockquotes.
 */
export function rehypeMarkUnclosedFences() {
  return (tree: HastNode, file: { value?: unknown }) => {
    if (typeof file.value !== "string") return;
    const source = file.value;
    const pending: HastNode[] = [tree];
    while (pending.length > 0) {
      const node = pending.pop()!;
      const start = node.position?.start.offset;
      const end = node.position?.end.offset;
      if (node.tagName === "pre" && start !== undefined && end !== undefined) {
        const lineStart = source.lastIndexOf("\n", start - 1) + 1;
        if (!isFenceClosed(source.slice(start, end), source.slice(lineStart, start))) {
          node.properties = { ...node.properties, dataUnclosedFence: true };
        }
        continue;
      }
      for (const child of node.children ?? []) pending.push(child);
    }
  };
}
