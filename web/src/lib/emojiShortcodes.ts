import remarkEmoji from "remark-emoji";
import { unified } from "unified";

// The Markdown preview's emoji table, so plain text resolves the same names
// without bundling a second emoji dataset.
const emojiShortcodeProcessor = unified().use(remarkEmoji);

// A backtick run closed by the next run of the same length, like a Markdown code span.
const INLINE_CODE_SPAN = /(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)/g;

/**
 * Replace GitHub/Slack-style `:shortcode:` emoji in plain text with their
 * glyphs, e.g. `"ship it :rocket:"` becomes `"ship it 🚀"`. Unknown names and
 * backtick code spans stay as written.
 */
export function replaceEmojiShortcodes(text: string): string {
  if (!text.includes(":")) return text;
  const children: { type: "text" | "inlineCode"; value: string }[] = [];
  let textStart = 0;
  for (const span of text.matchAll(INLINE_CODE_SPAN)) {
    if (span.index > textStart) {
      children.push({ type: "text", value: text.slice(textStart, span.index) });
    }
    // remark-emoji rewrites only text nodes, so the span keeps its literal text.
    children.push({ type: "inlineCode", value: span[0] });
    textStart = span.index + span[0].length;
  }
  if (textStart < text.length) children.push({ type: "text", value: text.slice(textStart) });
  const tree = emojiShortcodeProcessor.runSync({ type: "root", children });
  return tree.children.map((node) => ("value" in node ? node.value : "")).join("");
}
