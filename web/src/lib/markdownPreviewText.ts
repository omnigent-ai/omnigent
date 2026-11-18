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
// What starts a line's block: quote markers, then a heading's hashes or a list
// item's marker and its task box.
const BLOCK_MARKERS =
  /^[ \t]*(?:>[ \t]?)*[ \t]*(?:#{1,6}(?=[ \t]|$)|(?:[-*+]|\d{1,9}[.)])(?=[ \t]|$)(?:[ \t]+\[[ xX]\](?=[ \t]|$))?)?/;
// A code span: a run of backticks, its code, then a run of as many.
const CODE_SPAN = /(?<!`)(`+)(?!`)(.*?[^`])\1(?!`)/g;
// Code spans wait behind placeholders while the rules below run, so their
// characters stay as written. Agents don't write private-use characters.
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
  [/(?<![\\*_\w])(\*\*|__)(?=\S)(.*?\S)\1(?![*_\w])/g, "$2"],
  [/(?<![\\*_\w])([*_])(?=[^\s*_])(.*?[^\s\\])\1(?![*_\w])/g, "$2"],
  [/(?<![\\~])~~(?=\S)(.*?\S)~~(?!~)/g, "$1"],
  // An escaped character shows as itself.
  [/\\([\\`*_{}[\]()#+.!|<>~-])/g, "$1"],
];

/** One line's inline Markdown as plain text. */
function inlinePlainText(line: string): string {
  const codes: string[] = [];
  let text = line.replace(CODE_SPAN, (_span, _ticks: string, code: string) => {
    codes.push(code);
    return `${PLACEHOLDER}${codes.length - 1}${PLACEHOLDER}`;
  });
  for (const [pattern, replacement] of INLINE_RULES) text = text.replace(pattern, replacement);
  return text.replace(PLACEHOLDER_PATTERN, (_placeholder, index: string) => codes[Number(index)]);
}

/**
 * Markdown as plain text on one line, for a preview that cuts it short: the
 * syntax goes and the text stays. Code keeps its characters, a link its label
 * and a table its cells. Each line follows the one before after a space, or
 * after " · " when that one ends without punctuation, as a list item may.
 *
 * @param markdown e.g. `"**Inputs:** the pull request\n\n- Fix the tests\n- Push"`.
 * @returns e.g. `"Inputs: the pull request · Fix the tests · Push"`; empty when no text remains.
 */
export function markdownPreviewText(markdown: string): string {
  const lines: string[] = [];
  let fence: string | null = null;
  for (const line of markdown.slice(0, PREVIEW_SOURCE_MAX_CHARS).split(/\r\n?|\n/)) {
    const fenceLine = FENCE_LINE.exec(line);
    if (fence !== null) {
      // A closing fence repeats the opening character at least as many times.
      const closes =
        fenceLine !== null &&
        fenceLine[1][0] === fence[0] &&
        fenceLine[1].length >= fence.length &&
        fenceLine[2].trim() === "";
      if (closes) fence = null;
      else lines.push(line);
    } else if (fenceLine) {
      fence = fenceLine[1];
    } else if (!RULE_LINE.test(line) && !TABLE_DELIMITER_LINE.test(line)) {
      const row = TABLE_ROW_LINE.exec(line);
      lines.push(
        row
          ? row[1]
              .split("|")
              .map((cell) => inlinePlainText(cell).trim())
              .filter(Boolean)
              .join(" · ")
          : inlinePlainText(line.replace(BLOCK_MARKERS, "")),
      );
    }
  }
  let text = "";
  for (const line of lines) {
    const plain = line.replace(/\s+/g, " ").trim();
    if (!plain) continue;
    if (!text) text = plain;
    else text += `${ENDS_WITH_PUNCTUATION.test(text) ? " " : " · "}${plain}`;
  }
  return text;
}
