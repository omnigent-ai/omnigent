import remarkEmoji from "remark-emoji";
import { unified } from "unified";

// The Markdown preview's emoji table, so plain text resolves the same names
// without bundling a second emoji dataset.
const emojiShortcodeProcessor = unified().use(remarkEmoji);

// A fence line: up to three spaces, then three or more backticks or tildes.
const CODE_FENCE_LINE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
// A backtick run closed by the next run of the same length, like a Markdown code span.
const INLINE_CODE_SPAN = /(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)/g;

interface TextSegment {
  type: "text" | "inlineCode";
  value: string;
}

/**
 * Split out fenced code blocks by Markdown's rules: a block closes on a line
 * with at least as many of the same fence character, or runs to the end.
 */
function splitFencedCode(text: string): TextSegment[] {
  const segments: TextSegment[] = [];
  let openFence: string | null = null;
  let current = "";
  const flush = (type: TextSegment["type"]) => {
    if (current) segments.push({ type, value: current });
    current = "";
  };
  for (const line of text.split(/(?<=\n)/)) {
    const [, fence, rest = ""] = CODE_FENCE_LINE.exec(line.replace(/\r?\n$/, "")) ?? [];
    // A backtick fence's info string can't contain a backtick; that line is an inline span.
    if (openFence === null && fence && !(fence[0] === "`" && rest.includes("`"))) {
      flush("text");
      openFence = fence;
      current = line;
    } else if (
      openFence !== null &&
      fence?.[0] === openFence[0] &&
      fence.length >= openFence.length &&
      rest.trim() === ""
    ) {
      current += line;
      flush("inlineCode");
      openFence = null;
    } else {
      current += line;
    }
  }
  flush(openFence === null ? "text" : "inlineCode");
  return segments;
}

function splitInlineCode(text: string): TextSegment[] {
  const segments: TextSegment[] = [];
  let textStart = 0;
  for (const span of text.matchAll(INLINE_CODE_SPAN)) {
    if (span.index > textStart) {
      segments.push({ type: "text", value: text.slice(textStart, span.index) });
    }
    segments.push({ type: "inlineCode", value: span[0] });
    textStart = span.index + span[0].length;
  }
  if (textStart < text.length) segments.push({ type: "text", value: text.slice(textStart) });
  return segments;
}

/**
 * Replace GitHub/Slack-style `:shortcode:` emoji in plain text with their
 * glyphs, e.g. `"ship it :rocket:"` becomes `"ship it 🚀"`. Unknown names,
 * code spans and fenced code blocks stay as written.
 */
export function replaceEmojiShortcodes(text: string): string {
  if (!text.includes(":")) return text;
  // remark-emoji rewrites only text nodes, so code keeps its literal text.
  const children = splitFencedCode(text).flatMap((segment) =>
    segment.type === "text" ? splitInlineCode(segment.value) : [segment],
  );
  const tree = emojiShortcodeProcessor.runSync({ type: "root", children });
  return tree.children.map((node) => ("value" in node ? node.value : "")).join("");
}
