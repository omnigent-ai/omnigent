// Find over a diff review's text. Matching runs on the review model's lines
// rather than the DOM, so files the virtualized list hasn't rendered, including
// collapsed ones, are found too.

import type { FindOptions } from "@/components/FindBar";

/** One line find searches: a diff line on one side, or a file's path. */
export interface ReviewSearchLine {
  /** The review item the line belongs to. */
  id: string;
  text: string;
  lineNumber: number;
  side?: "additions" | "deletions";
  /** A path, or a rich Markdown diff's section, numbered by `lineNumber` from 1. */
  target?: "currentPath" | "previousPath" | "markdownPreview";
}

/** One occurrence of the query within a line, as a character range. */
export interface ReviewSearchMatch extends ReviewSearchLine {
  from: number;
  to: number;
}

export interface ReviewFindResult {
  /** False when a regular expression query doesn't compile. */
  valid: boolean;
  /** True when matching stopped at `MAXIMUM_FIND_MATCHES`. */
  truncated: boolean;
  matches: ReviewSearchMatch[];
}

export const MAXIMUM_FIND_MATCHES = 20_000;

const WORD_CHARACTER = /[\p{L}\p{N}_]/u;

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function isWordBoundary(text: string, index: number): boolean {
  const before = index > 0 && WORD_CHARACTER.test(text[index - 1]!);
  const after = index < text.length && WORD_CHARACTER.test(text[index]!);
  return before !== after;
}

function withoutLineEnding(text: string): string {
  if (text.endsWith("\r\n")) return text.slice(0, -2);
  if (text.endsWith("\n") || text.endsWith("\r")) return text.slice(0, -1);
  return text;
}

/** Compile the query, or return null when a regular expression is invalid. */
export function compileFindQuery(query: string, options: FindOptions): RegExp | null {
  const source = options.regexp ? query : escapeRegExp(query);
  try {
    return new RegExp(source, options.caseSensitive ? "gu" : "giu");
  } catch {
    return null;
  }
}

/**
 * Every match of `query` across `lines`, in order. Empty matches (such as a
 * regex `^`) are skipped, and a whole-word query needs a word boundary at both
 * ends of the match.
 */
export function findReviewMatches(
  lines: readonly ReviewSearchLine[],
  query: string,
  options: FindOptions,
): ReviewFindResult {
  if (!query) return { valid: true, truncated: false, matches: [] };
  const expression = compileFindQuery(query, options);
  if (!expression) return { valid: false, truncated: false, matches: [] };
  const matches: ReviewSearchMatch[] = [];
  for (const line of lines) {
    const text = withoutLineEnding(line.text);
    expression.lastIndex = 0;
    for (let found = expression.exec(text); found; found = expression.exec(text)) {
      const from = found.index;
      const to = from + found[0].length;
      if (to === from) {
        // Step over a whole code point so a `u` expression can't match here again.
        const codePoint = text.codePointAt(from);
        expression.lastIndex = from + (codePoint !== undefined && codePoint > 0xffff ? 2 : 1);
        continue;
      }
      if (options.wholeWord && !(isWordBoundary(text, from) && isWordBoundary(text, to))) {
        continue;
      }
      matches.push({ ...line, from, to });
      if (matches.length === MAXIMUM_FIND_MATCHES) {
        return { valid: true, truncated: true, matches };
      }
    }
  }
  return { valid: true, truncated: false, matches };
}

/** The find bar's status: "3 of 12", "No results", or empty without a query. */
export function findStatusLabel(
  query: string,
  result: ReviewFindResult,
  activeIndex: number,
): string {
  if (!query) return "";
  if (!result.valid) return "Invalid expression";
  if (result.matches.length === 0) return "No results";
  const position = Math.min(activeIndex + 1, result.matches.length);
  return `${position} of ${result.matches.length}${result.truncated ? "+" : ""}`;
}

/** A selection's text as a single-line query: line breaks become spaces. */
export function findQueryFromSelection(text: string): string {
  return text.replace(/\s*\r?\n\s*/g, " ").trim();
}
