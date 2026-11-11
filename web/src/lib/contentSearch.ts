// Turns the Content search options into the query the runner's content-search
// route already understands (`q` plus `regex`), so Whole word needs no runner
// change. The runner searches with ripgrep, whose regex dialect this targets.

/** The longest `q` the server and runner accept, counted in code points. */
export const CONTENT_SEARCH_QUERY_LIMIT = 4096;

/** Characters ripgrep's regex syntax treats as special, escaped in a literal query. */
const REGEX_SPECIAL = /[\\^$.*+?()[\]{}|#&~-]/g;
/** An ASCII word character, which every Unicode version, and so ripgrep's `\w`, agrees on. */
const ASCII_WORD_CHARACTER = /^[A-Za-z0-9_]$/;

/**
 * The pattern for a literal's first or last character `character`. An ASCII
 * character is classified here; a word character gets a boundary on `side`.
 * The browser's Unicode tables can differ from the runner's ripgrep for any
 * other character, so ripgrep classifies it: `[c&&\w]` and `[c&&\W]` each match
 * `c` only when ripgrep counts it as a word or a non-word character, and only
 * the word alternative needs the boundary. Both consume just `c`, so the
 * reported match is still the literal itself.
 */
function edgePattern(character: string, side: { start: boolean; end: boolean }): string {
  const start = side.start ? "\\b" : "";
  const end = side.end ? "\\b" : "";
  if (ASCII_WORD_CHARACTER.test(character)) return `${start}${character}${end}`;
  const codePoint = character.codePointAt(0) ?? 0;
  // Every Unicode version agrees that any other ASCII character is not a word character.
  if (codePoint < 0x80) return escapeLiteral(character);
  const code = `\\x{${codePoint.toString(16).toUpperCase()}}`;
  return `(?:${start}[${code}&&\\w]${end}|[${code}&&\\W])`;
}

function escapeLiteral(text: string): string {
  return text.replace(REGEX_SPECIAL, "\\$&");
}

export interface ContentSearchPatternInput {
  query: string;
  regex: boolean;
  wholeWord: boolean;
}

/**
 * The `q` and `regex` values to send. With Whole word off they are the input
 * unchanged. A regular expression is wrapped in `\b(?:…)\b`. A literal is
 * escaped, and needs a word boundary only on a side where it starts or ends
 * with a word character, so `C++` and `(needle)` still match as typed. Which
 * characters are word characters follows the runner's ripgrep (see `edgePattern`).
 */
export function contentSearchPattern({ query, regex, wholeWord }: ContentSearchPatternInput): {
  pattern: string;
  regex: boolean;
} {
  if (!wholeWord) return { pattern: query, regex };
  if (regex) return { pattern: `\\b(?:${query})\\b`, regex: true };
  const characters = Array.from(query);
  if (characters.length === 0) return { pattern: "", regex: true };
  if (characters.length === 1) {
    return { pattern: edgePattern(characters[0], { start: true, end: true }), regex: true };
  }
  const first = edgePattern(characters[0], { start: true, end: false });
  const middle = escapeLiteral(characters.slice(1, -1).join(""));
  const last = edgePattern(characters[characters.length - 1], { start: false, end: true });
  return { pattern: `${first}${middle}${last}`, regex: true };
}

/** Why Whole word can't wrap a regular expression in `\b(?:…)\b`. */
export type WholeWordRegexProblem = "unopened-group" | "verbose";

/**
 * Whether a ripgrep regular expression could escape the `\b(?:…)\b` Whole word
 * wrapper, leaving an alternative without its boundary.
 *
 * - `unopened-group`: it closes a group it never opened, as in `foo)|(bar`.
 *   Ripgrep accepts that, so wrapped, it would close the wrapper instead.
 * - `verbose`: it turns on verbose mode, as in `(?x)`, where ignored whitespace
 *   and comments change how ripgrep reads brackets. Rather than model that,
 *   such an expression is refused.
 *
 * Escapes, (nested) character classes and capture-group names, which may
 * contain `[` and `]`, are skipped as ripgrep reads them. That is exact up to
 * the first flag group that could turn on verbose mode.
 */
export function wholeWordRegexProblem(query: string): WholeWordRegexProblem | null {
  let groups = 0;
  let classes = 0;
  // Just after `[` or `[^`, where a `]` is a literal rather than the end of the class.
  let classStart = false;
  for (let index = 0; index < query.length; index += 1) {
    const character = query[index];
    if (character === "\\") {
      index += 1;
      classStart = false;
    } else if (character === "[") {
      classes += 1;
      classStart = true;
    } else if (classes > 0) {
      if (character === "]" && !classStart) classes -= 1;
      classStart = classStart && character === "^" && query[index - 1] === "[";
    } else if (character === "(") {
      const header = query.slice(index + 1);
      // A named group, `(?P<name>…)` or `(?<name>…)`: the name is not regex syntax.
      const name = header.match(/^\?P?<[^>]*>/);
      if (name) index += name[0].length;
      // A flag group, `(?flags)` or `(?flags:…)`; the flags before any `-` are turned on.
      const flags = header.match(/^\?([A-Za-z-]*)[):]/);
      if (flags?.[1].split("-")[0].includes("x")) return "verbose";
      groups += 1;
    } else if (character === ")") {
      groups -= 1;
      if (groups < 0) return "unopened-group";
    }
  }
  return null;
}

/** Whether `pattern` fits the server's limit, counting code points as the server does. */
export function fitsContentSearchLimit(pattern: string): boolean {
  return Array.from(pattern).length <= CONTENT_SEARCH_QUERY_LIMIT;
}
