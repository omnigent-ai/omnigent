// Turns the Content search options into the query the runner's content-search
// route already understands (`q` plus `regex`), so Whole word needs no runner
// change. The runner searches with ripgrep, whose regex dialect this targets.

import { authenticatedFetch } from "@/lib/identity";
import { searchTextViaWorkspaceApi, type TextSearchOptions } from "@/lib/workspaceApi";

/** One matching line, for its first match; offsets count UTF-16 code units. */
export interface ContentMatch {
  path: string;
  line: number;
  column: number;
  text: string;
  match_start: number;
  match_end: number;
}

export interface ContentSearchResult {
  matches: ContentMatch[];
  truncated: boolean;
  warning: string | null;
}

/** A content search as typed, with its options; the same shape wherever it's typed. */
export interface ContentSearchInput {
  query: string;
  caseSensitive: boolean;
  regex: boolean;
  /** Optional so an input cached before this option existed still reads as off. */
  wholeWord?: boolean;
  include: string;
  exclude: string;
}

export const EMPTY_CONTENT_SEARCH: ContentSearchInput = {
  query: "",
  caseSensitive: false,
  regex: false,
  wholeWord: false,
  include: "",
  exclude: "",
};

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

/**
 * Why `input` can't be searched as typed, as a sentence that says how to fix it, or null.
 * Whole word sends a regular expression, which can outgrow the server's limit, and some
 * expressions would escape its boundaries.
 */
export function contentSearchProblem(input: ContentSearchInput): string | null {
  if (input.wholeWord !== true) return null;
  const { pattern } = contentSearchPattern({ ...input, wholeWord: true });
  if (!fitsContentSearchLimit(pattern)) {
    return "This search is too long to match whole words. Shorten it, or turn off Whole word.";
  }
  const problem = input.regex ? wholeWordRegexProblem(input.query) : null;
  if (problem === "unopened-group") {
    return "This expression closes a group it never opened, so it can’t match whole words. Balance its parentheses, or turn off Whole word.";
  }
  if (problem === "verbose") {
    return "Whole word can’t be used with verbose mode (?x). Remove that flag, or turn off Whole word.";
  }
  return null;
}

/** Runs `input` as typed against the session's workspace; see {@link fetchContentSearch}. */
export function runContentSearch(
  conversationId: string,
  input: ContentSearchInput,
  signal?: AbortSignal,
): Promise<ContentSearchResult> {
  const sent = contentSearchPattern({ ...input, wholeWord: input.wholeWord === true });
  return fetchContentSearch(
    conversationId,
    sent.pattern,
    {
      caseSensitive: input.caseSensitive,
      regex: sent.regex,
      include: input.include,
      exclude: input.exclude,
    },
    signal,
  );
}

/**
 * Search a session's workspace contents: through the workspace API when a
 * route serves it, else through the REST content-search route.
 *
 * @param pattern The query to send, from {@link contentSearchPattern}.
 * @throws Error with a user-facing message when the search can't run.
 */
export async function fetchContentSearch(
  conversationId: string,
  pattern: string,
  options: TextSearchOptions,
  signal?: AbortSignal,
): Promise<ContentSearchResult> {
  const viaApi = await searchTextViaWorkspaceApi(conversationId, pattern, options, signal);
  if (viaApi) return viaApi;
  const params = new URLSearchParams({
    q: pattern,
    case_sensitive: String(options.caseSensitive),
    regex: String(options.regex),
    include: options.include,
    exclude: options.exclude,
  });
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/resources/environments/default/content-search?${params}`,
    { signal },
  );
  const body = await response.json().catch(() => null);
  if (!response.ok || body?.error || !Array.isArray(body?.matches)) {
    throw new Error(
      (typeof body?.error === "string" ? body.error : body?.error?.message) ||
        (typeof body?.detail === "string" ? body.detail : null) ||
        "Content search is unavailable. Check that the runner is online and up to date.",
    );
  }
  return body as ContentSearchResult;
}

/** A word character as the search engines' Unicode `\w` counts one, per UTS #18. */
const WORD_CHARACTER = /[\p{Alphabetic}\p{M}\p{Nd}\p{Pc}\p{Join_Control}]/u;

/** A half-open range of UTF-16 offsets into a match's `text`. */
export interface ContentMatchRange {
  start: number;
  end: number;
}

const CROP_MARK = "…";

function characterAt(text: string, index: number): string {
  const codePoint = text.codePointAt(index);
  return codePoint === undefined ? "" : String.fromCodePoint(codePoint);
}

function characterBefore(text: string, index: number): string {
  return Array.from(text.slice(Math.max(0, index - 2), index)).at(-1) ?? "";
}

/**
 * Every occurrence of a plain-text query in a matching line, starting with the
 * one the search reported. A regular expression keeps only the reported range,
 * since the engines' dialects differ and a user's pattern could stall the UI.
 */
export function contentMatchRanges(
  match: Pick<ContentMatch, "text" | "match_start" | "match_end">,
  input: Pick<ContentSearchInput, "query" | "caseSensitive" | "regex" | "wholeWord">,
): ContentMatchRange[] {
  const reported =
    match.match_end > match.match_start ? [{ start: match.match_start, end: match.match_end }] : [];
  if (input.regex || input.query.length === 0) return reported;
  const characters = Array.from(input.query);
  const boundaryBefore = input.wholeWord === true && WORD_CHARACTER.test(characters[0]);
  const boundaryAfter = input.wholeWord === true && WORD_CHARACTER.test(characters.at(-1) ?? "");
  // Unicode-mode patterns reject needless escapes, so only syntax characters are escaped.
  const literal = input.query.replace(/[\\^$.*+?()[\]{}|/]/g, "\\$&");
  const pattern = new RegExp(literal, input.caseSensitive ? "gu" : "giu");
  const { text } = match;
  // A cropped line's neighbors are unknown, so a whole word can't be confirmed at the crop.
  const croppedStart = text.startsWith(CROP_MARK) ? CROP_MARK.length : -1;
  const croppedEnd = text.endsWith(CROP_MARK) ? text.length - CROP_MARK.length : -1;
  const ranges = [...reported];
  let found = pattern.exec(text);
  while (found) {
    const start = found.index;
    const end = start + found[0].length;
    // Boundaries are checked by hand because older Safari lacks lookbehind.
    const isWhole =
      (!boundaryBefore ||
        (start !== croppedStart && !WORD_CHARACTER.test(characterBefore(text, start)))) &&
      (!boundaryAfter || (end !== croppedEnd && !WORD_CHARACTER.test(characterAt(text, end))));
    if (!isWhole) {
      // A rejected candidate may hide a whole word that overlaps it, so retry just past its start.
      pattern.lastIndex = start + characterAt(text, start).length;
    } else if (!reported.some((range) => start < range.end && end > range.start)) {
      ranges.push({ start, end });
    }
    found = pattern.exec(text);
  }
  return ranges.sort((first, second) => first.start - second.start);
}
