import remarkEmoji from "remark-emoji";
import remarkParse from "remark-parse";
import { unified } from "unified";

const markdownParser = unified().use(remarkParse);
// The Markdown preview's emoji table, so plain text resolves the same names
// without bundling a second emoji dataset.
const emojiShortcodeProcessor = unified().use(remarkEmoji);

/**
 * Longer bodies render as written: Markdown parsing is superlinear on some
 * crafted inputs, and this keeps its worst case to a few tens of milliseconds.
 */
export const MAX_EMOJI_SHORTCODE_TEXT_LENGTH = 10_000;

interface SourceNode {
  type: string;
  position?: { start: { offset?: number }; end: { offset?: number } };
  children?: SourceNode[];
}

/** Source offsets of every code span and code block, in document order. */
function collectCodeRanges(node: SourceNode, ranges: [number, number][]): [number, number][] {
  const start = node.position?.start.offset;
  const end = node.position?.end.offset;
  if ((node.type === "code" || node.type === "inlineCode") && start != null && end != null) {
    ranges.push([start, end]);
  } else {
    for (const child of node.children ?? []) collectCodeRanges(child, ranges);
  }
  return ranges;
}

/**
 * Replace GitHub/Slack-style `:shortcode:` emoji in plain text with their
 * glyphs, e.g. `"ship it :rocket:"` becomes `"ship it 🚀"`. Unknown names and
 * anything Markdown would treat as code stay as written.
 */
export function replaceEmojiShortcodes(text: string): string {
  if (!text.includes(":") || text.length > MAX_EMOJI_SHORTCODE_TEXT_LENGTH) return text;
  const children: { type: "text" | "inlineCode"; value: string }[] = [];
  let textStart = 0;
  for (const [start, end] of collectCodeRanges(markdownParser.parse(text), [])) {
    if (start > textStart) children.push({ type: "text", value: text.slice(textStart, start) });
    // remark-emoji rewrites only text nodes, so code keeps its source text.
    children.push({ type: "inlineCode", value: text.slice(start, end) });
    textStart = end;
  }
  if (textStart < text.length) children.push({ type: "text", value: text.slice(textStart) });
  const tree = emojiShortcodeProcessor.runSync({ type: "root", children });
  return tree.children.map((node) => ("value" in node ? node.value : "")).join("");
}
