// Go to File's text results: each matching line under a heading for its file, in the order
// the search found them, with every match marked and the line number alongside.

import type { QuickPickItem } from "@/components/ui/quick-pick";
import {
  type ContentMatch,
  contentMatchRanges,
  type ContentSearchInput,
  type ContentSearchResult,
} from "@/lib/contentSearch";

import type { GoToFileTarget } from "./goToFileItems";

/** What was searched for, which decides the line's highlights. */
export type TextSearchQuery = Pick<
  ContentSearchInput,
  "query" | "caseSensitive" | "regex" | "wholeWord"
>;

function lineItem(match: ContentMatch, query: TextSearchQuery): QuickPickItem<GoToFileTarget> {
  // The line shows without its indentation, so the marks move left with it.
  const text = match.text.trimStart();
  const removed = match.text.length - text.length;
  const ranges = contentMatchRanges(match, query)
    .map((range) => ({
      start: Math.max(0, range.start - removed),
      end: Math.max(0, range.end - removed),
    }))
    .filter((range) => range.end > range.start);
  return {
    id: `line:${match.path}:${match.line}:${match.column}`,
    label: text || " ",
    detail: String(match.line),
    group: match.path,
    highlights: { label: ranges },
    matchHighlight: "strong",
    rememberInHistory: false,
    value: {
      path: match.path,
      line: match.line,
      column: match.column,
      // A match cropped from the line's end stops at the crop.
      endColumn: match.column + Math.max(0, match.match_end - match.match_start),
    },
  };
}

/** One row per matching line, grouped under its file in the order files were first found. */
export function textSearchItems(
  matches: readonly ContentMatch[],
  query: TextSearchQuery,
): QuickPickItem<GoToFileTarget>[] {
  return matches.map((match) => lineItem(match, query));
}

/** The footer's count, such as "500+ results in 32 files". */
export function textSearchSummary(
  result: Pick<ContentSearchResult, "matches" | "truncated">,
): string {
  const count = result.matches.length;
  const files = new Set(result.matches.map((match) => match.path)).size;
  const results = `${count.toLocaleString()}${result.truncated ? "+" : ""} ${count === 1 && !result.truncated ? "result" : "results"}`;
  return `${results} in ${files.toLocaleString()} ${files === 1 ? "file" : "files"}`;
}
