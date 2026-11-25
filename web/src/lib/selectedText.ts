// The text the user has selected, as a search query.

/** Longest selection a search query takes, as the search routes accept. */
const MAXIMUM_QUERY_LENGTH = 4096;

/** A selection's first non-blank line, trimmed, as a search query; null when it makes none. */
export function searchQueryFromSelection(selection: string): string | null {
  const firstLine = selection.split(/\r\n?|\n/).find((line) => line.trim() !== "") ?? "";
  const text = firstLine.trim();
  return text && text.length <= MAXIMUM_QUERY_LENGTH ? text : null;
}
