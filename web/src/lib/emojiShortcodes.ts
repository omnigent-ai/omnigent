import remarkEmoji from "remark-emoji";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";

// GFM so bare URLs parse as links and keep their text.
const markdownParser = unified().use(remarkParse).use(remarkGfm);
// The Markdown preview's emoji table, so plain text resolves the same names
// without bundling a second emoji dataset.
const emojiShortcodeProcessor = unified().use(remarkEmoji);

/**
 * Longer bodies render as written: Markdown parsing is superlinear on some
 * crafted inputs (e.g. long emphasis or bracket runs), and this keeps its
 * worst case near 20 ms.
 */
export const MAX_EMOJI_SHORTCODE_TEXT_LENGTH = 2_000;

/** Nodes whose source must stay as typed: code, raw HTML, and link syntax or URLs. */
const LITERAL_NODE_TYPES = new Set([
  "code",
  "inlineCode",
  "html",
  "link",
  "linkReference",
  "image",
  "imageReference",
  "definition",
]);

interface SourceNode {
  type: string;
  position?: { start: { offset?: number }; end: { offset?: number } };
  children?: SourceNode[];
}

/** Source offsets of every literal node, in document order. */
function collectLiteralRanges(node: SourceNode, ranges: [number, number][]): [number, number][] {
  const start = node.position?.start.offset;
  const end = node.position?.end.offset;
  if (LITERAL_NODE_TYPES.has(node.type) && start !== undefined && end !== undefined) {
    ranges.push([start, end]);
  } else {
    for (const child of node.children ?? []) collectLiteralRanges(child, ranges);
  }
  return ranges;
}

/**
 * Replace GitHub/Slack-style `:shortcode:` emoji in plain text with their
 * glyphs, e.g. `"ship it :rocket:"` becomes `"ship it 🚀"`. Unknown names,
 * code, raw HTML tags, links and URLs stay as written.
 */
export function replaceEmojiShortcodes(text: string): string {
  if (!text.includes(":") || text.length > MAX_EMOJI_SHORTCODE_TEXT_LENGTH) return text;
  const children: { type: "text" | "inlineCode"; value: string }[] = [];
  let textStart = 0;
  for (const [start, end] of collectLiteralRanges(markdownParser.parse(text), [])) {
    if (start > textStart) children.push({ type: "text", value: text.slice(textStart, start) });
    // remark-emoji rewrites only text nodes, so this keeps its source text.
    children.push({ type: "inlineCode", value: text.slice(start, end) });
    textStart = end;
  }
  if (textStart < text.length) children.push({ type: "text", value: text.slice(textStart) });
  const tree = emojiShortcodeProcessor.runSync({ type: "root", children });
  return tree.children.map((node) => ("value" in node ? node.value : "")).join("");
}
