// The text the user has selected, as a search query.

import { CONTENT_SEARCH_QUERY_LIMIT } from "@/lib/contentSearch";

/** A selection's first non-blank line, trimmed, as a search query; null when it makes none. */
export function searchQueryFromSelection(selection: string): string | null {
  const firstLine = selection.split(/\r\n?|\n/).find((line) => line.trim() !== "") ?? "";
  const text = firstLine.trim();
  return text && text.length <= CONTENT_SEARCH_QUERY_LIMIT ? text : null;
}
