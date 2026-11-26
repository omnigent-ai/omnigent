// The text the user has selected, as a search query.

import { CONTENT_SEARCH_QUERY_LIMIT, fitsContentSearchLimit } from "@/lib/contentSearch";

/** A selection's first non-blank line, trimmed, as a search query; null when it makes none. */
export function searchQueryFromSelection(selection: string): string | null {
  const firstLine = selection.split(/\r\n?|\n/).find((line) => line.trim() !== "") ?? "";
  const text = firstLine.trim();
  // Every character is at most two UTF-16 units, so a longer line can't fit.
  if (!text || text.length > 2 * CONTENT_SEARCH_QUERY_LIMIT) return null;
  return fitsContentSearchLimit(text) ? text : null;
}
