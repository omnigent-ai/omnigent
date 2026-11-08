// Turns the Content search options into the query the runner's content-search
// route already understands (`q` plus `regex`), so Whole word needs no runner
// change. The runner searches with ripgrep, whose regex dialect this targets.

/** The longest `q` the server and runner accept, counted in code points. */
export const CONTENT_SEARCH_QUERY_LIMIT = 4096;

/** Characters ripgrep's regex syntax treats as special, escaped in a literal query. */
const REGEX_SPECIAL = /[\\^$.*+?()[\]{}|#&~-]/g;
/** A word character as ripgrep's Unicode `\w` counts one (UTS #18). */
const WORD_CHARACTER = /[\p{Alphabetic}\p{M}\p{Nd}\p{Pc}\p{Join_Control}]/u;

export interface ContentSearchPatternInput {
  query: string;
  regex: boolean;
  wholeWord: boolean;
}

/**
 * The `q` and `regex` values to send. With Whole word off they are the input
 * unchanged. A regular expression is wrapped in `\b(?:…)\b`. A literal is
 * escaped, and needs a word boundary only on a side where it starts or ends
 * with a word character, so `C++` and `(needle)` still match as typed.
 */
export function contentSearchPattern({ query, regex, wholeWord }: ContentSearchPatternInput): {
  pattern: string;
  regex: boolean;
} {
  if (!wholeWord) return { pattern: query, regex };
  if (regex) return { pattern: `\\b(?:${query})\\b`, regex: true };
  const characters = Array.from(query);
  const start = WORD_CHARACTER.test(characters[0] ?? "") ? "\\b" : "";
  const end = WORD_CHARACTER.test(characters.at(-1) ?? "") ? "\\b" : "";
  return { pattern: `${start}${query.replace(REGEX_SPECIAL, "\\$&")}${end}`, regex: true };
}

/** Whether `pattern` fits the server's limit, counting code points as the server does. */
export function fitsContentSearchLimit(pattern: string): boolean {
  return Array.from(pattern).length <= CONTENT_SEARCH_QUERY_LIMIT;
}
