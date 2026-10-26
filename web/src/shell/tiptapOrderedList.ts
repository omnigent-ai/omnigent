import { ORDERED_LIST_MARKER_PATTERN, OrderedList } from "@tiptap/extension-list";

// Same item pattern as the upstream ordered-list tokenizer.
const ORDERED_LIST_ITEM = new RegExp(`^(\\s*)(${ORDERED_LIST_MARKER_PATTERN})([.)])\\s+(.*)$`);

// Upstream strips `indent + marker + 1` columns from continuation lines, which
// leaves the space after `1.`; remove that gap first so its strip reaches the
// item's content column. Lines stay indented, so item boundaries are unchanged.
function alignContinuationLines(source: string): string {
  let upstreamColumn = 0;
  let markerGap = 0;
  return source
    .split("\n")
    .map((line) => {
      const item = ORDERED_LIST_ITEM.exec(line);
      if (item) {
        const [, indent, marker, , content] = item;
        upstreamColumn = indent.length + marker.length + 1;
        // CommonMark: after a blank or 5+ space gap, content starts one column past the marker.
        const gap = line.length - content.length - upstreamColumn;
        markerGap = content && gap <= 4 ? gap : 1;
        return line;
      }
      const leading = line.length - line.trimStart().length;
      return line.slice(Math.min(markerGap, Math.max(0, leading - upstreamColumn)));
    })
    .join("\n");
}

// List extent depends only on line structure, so probing it skips content lexing.
const PROBE_LEXER = { inlineTokens: () => [], blockTokens: () => [] };

// TODO: Remove when upstream strips the full marker width from ordered-list
// continuation lines.
const orderedListTokenizer = OrderedList.config.markdownTokenizer!;

export const AlignedOrderedList = OrderedList.extend({
  markdownTokenizer: {
    ...orderedListTokenizer,
    tokenize(source, tokens, lexer) {
      const extent = orderedListTokenizer.tokenize(source, tokens, PROBE_LEXER);
      if (extent?.raw === undefined) return extent;
      const aligned = alignContinuationLines(extent.raw);
      const token = orderedListTokenizer.tokenize(aligned, tokens, lexer);
      // Never skip source the aligned list did not consume.
      if (token?.raw !== aligned) return orderedListTokenizer.tokenize(source, tokens, lexer);
      // The lexer advances by `raw`, so it must be the original source.
      return { ...token, raw: extent.raw };
    },
  },
});
