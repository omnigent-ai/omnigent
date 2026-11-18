// Markdown as plain text, for a preview that shows only its start, such as a
// workflow step's two-line summary on its card.

// A preview shows a few lines at most, so it reads only this much of the source.
const PREVIEW_SOURCE_MAX_CHARS = 2000;

// A fence opens or closes a code block: three or more backticks or tildes.
const FENCE_LINE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
// A thematic break, or a setext heading's underline.
const RULE_LINE = /^ {0,3}(?:([-*_])(?:[ \t]*\1){2,}|=+)[ \t]*$/;
// A table's delimiter row, e.g. `| --- | :-: |`.
const TABLE_DELIMITER_LINE = /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$/;
// A table row: its cells between pipes.
const TABLE_ROW_LINE = /^[ \t]*\|(.*)\|[ \t]*$/;
// A table cell's text: anything but a pipe, unless a backslash escapes it.
const TABLE_CELL = /(?:\\.|[^|\\])+/g;
// A quote's markers, which a quoted paragraph repeats on each of its lines.
const QUOTE_MARKERS = /^[ \t]*(?:>[ \t]?)*/;
// What starts a block on a line: a heading's hashes, or a list item's marker
// and its task box.
const BLOCK_MARKER =
  /^[ \t]*(?:#{1,6}(?=[ \t]|$)|(?:[-*+]|\d{1,9}[.)])(?=[ \t]|$)(?:[ \t]+\[[ xX]\](?=[ \t]|$))?)/;
// A code span: a run of backticks that no backslash escapes, its code, then a
// run of as many. This rule and the ones below capture the character before a
// delimiter instead of looking behind, which Safari before 16.4 can't parse.
const CODE_SPAN = /(^|[^`\\])(`+)(?!`)(.*?[^`])\2(?!`)/g;
// Code spans wait behind placeholders while the rules below run, so their
// characters stay as written. A placeholder is a private-use character.
const PLACEHOLDER = String.fromCharCode(0xe000);
const PLACEHOLDER_PATTERN = new RegExp(`${PLACEHOLDER}(\\d+)${PLACEHOLDER}`, "g");
// A line that ends a sentence or a clause; the next one follows after a space.
const ENDS_WITH_PUNCTUATION = /[.!?:;,]["')\]]*$/;

const INLINE_RULES: readonly (readonly [RegExp, string])[] = [
  // An image shows its alt text, and a link its label.
  [/!\[([^\]]*)\]\((?:[^()\s]|\([^()\s]*\))*(?:[ \t]+"[^"]*")?\)/g, "$1"],
  [/\[([^\]]+)\]\((?:[^()\s]|\([^()\s]*\))*(?:[ \t]+"[^"]*")?\)/g, "$1"],
  [/\[([^\]]+)\]\[[^\]]*\]/g, "$1"],
  [/<((?:https?|mailto):[^\s<>]+)>/g, "$1"],
  // Strong, emphasis and strikethrough keep their text. Delimiters inside a
  // word, such as snake_case, stay.
  [/(^|[^\\*_\w])(\*\*|__)(?=\S)(.*?\S)\2(?![*_\w])/g, "$1$3"],
  [/(^|[^\\*_\w])([*_])(?=[^\s*_])(.*?[^\s\\])\2(?![*_\w])/g, "$1$3"],
  [/(^|[^\\~])~~(?=\S)(.*?\S)~~(?!~)/g, "$1$2"],
  // An escaped character shows as itself.
  [/\\([\\`*_{}[\]()#+.!|<>~-])/g, "$1"],
];

/** A block's inline Markdown as plain text. */
function inlinePlainText(source: string): string {
  const codes: string[] = [];
  // The source's own placeholder characters go first, so none reads as a placeholder.
  let text = source
    .replaceAll(PLACEHOLDER, "")
    .replace(CODE_SPAN, (_span, before: string, _ticks: string, code: string) => {
      codes.push(code);
      return `${before}${PLACEHOLDER}${codes.length - 1}${PLACEHOLDER}`;
    });
  for (const [pattern, replacement] of INLINE_RULES) text = text.replace(pattern, replacement);
  return text.replace(PLACEHOLDER_PATTERN, (_placeholder, index: string) => codes[Number(index)]);
}

/**
 * Markdown as plain text on one line, for a preview that cuts it short: the
 * syntax goes and the text stays. Code keeps its characters, a link its label
 * and a table its cells. A paragraph's lines join with spaces. Each block, such
 * as a paragraph, list item or heading, follows the one before after a space,
 * or after " · " when that one ends without punctuation.
 *
 * @param markdown e.g. `"**Inputs:** the pull request\n\n- Fix the tests\n- Push"`.
 * @returns e.g. `"Inputs: the pull request · Fix the tests · Push"`; empty when no text remains.
 */
export function markdownPreviewText(markdown: string): string {
  // Each block's text: a code line, a table row, or the source of a paragraph,
  // list item or heading, whose inline Markdown can span its lines.
  const blocks: { text: string; inline: boolean }[] = [];
  let fence: string | null = null;
  // Whether the line before ended its block: a blank line, heading, table row, rule or code.
  let blockEnded = true;
  for (const line of markdown.slice(0, PREVIEW_SOURCE_MAX_CHARS).split(/\r\n?|\n/)) {
    const fenceLine = FENCE_LINE.exec(line);
    const row = TABLE_ROW_LINE.exec(line);
    if (fence !== null) {
      // A closing fence repeats the opening character at least as many times.
      const closes =
        fenceLine !== null &&
        fenceLine[1][0] === fence[0] &&
        fenceLine[1].length >= fence.length &&
        fenceLine[2].trim() === "";
      if (closes) fence = null;
      else blocks.push({ text: line, inline: false });
      blockEnded = true;
    } else if (fenceLine) {
      fence = fenceLine[1];
      blockEnded = true;
    } else if (!line.trim() || RULE_LINE.test(line) || TABLE_DELIMITER_LINE.test(line)) {
      blockEnded = true;
    } else if (row) {
      // A cell's escaped pipes are pipes, even in its code.
      const cells = (row[1].match(TABLE_CELL) ?? []).map((cell) =>
        inlinePlainText(cell.replaceAll("\\|", "|")).trim(),
      );
      blocks.push({ text: cells.filter(Boolean).join(" · "), inline: false });
      blockEnded = true;
    } else {
      const unquoted = line.replace(QUOTE_MARKERS, "");
      const marker = BLOCK_MARKER.exec(unquoted)?.[0] ?? "";
      const content = unquoted.slice(marker.length);
      const last = blocks.at(-1);
      // A line without a marker goes on with the paragraph or list item before it.
      if (!blockEnded && marker === "" && last?.inline) last.text += ` ${content}`;
      else blocks.push({ text: content, inline: true });
      // A heading takes one line.
      blockEnded = marker.trimStart().startsWith("#");
    }
  }
  let text = "";
  for (const block of blocks) {
    const plain = (block.inline ? inlinePlainText(block.text) : block.text)
      .replace(/\s+/g, " ")
      .trim();
    if (!plain) continue;
    if (!text) text = plain;
    else text += `${ENDS_WITH_PUNCTUATION.test(text) ? " " : " · "}${plain}`;
  }
  return text;
}
