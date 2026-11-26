// What the Files tree's filters do right now, in words. A name searches the browsed
// folder, narrowed by the include and exclude globs; globs alone filter the tree itself.

import { splitGlobList } from "@/lib/globList";

export interface TreeFilterValues {
  nameContains: string;
  include: string;
  exclude: string;
}

export type TreeFilterSummary =
  /** No filter is narrowing the tree. */
  | { active: false }
  /**
   * A filter is on; `text` describes it. With `search`, the tree shows a name
   * search's matches; without it, the tree itself, narrowed by the globs.
   */
  | { active: true; search: boolean; text: string; emptyText: string };

export function treeFilterSummary({
  nameContains,
  include,
  exclude,
}: TreeFilterValues): TreeFilterSummary {
  const name = nameContains.trim();
  // Read as the filter parses them, so a list of only commas and blanks filters nothing.
  const included = splitGlobList(include).join(", ");
  const excluded = splitGlobList(exclude).join(", ");
  if (!name && !included && !excluded) return { active: false };
  if (!name) {
    const hiding = excluded && `hiding ${excluded}`;
    const text = included
      ? [`Only ${included}`, hiding].filter(Boolean).join(", ")
      : `Hiding ${excluded}`;
    return { active: true, search: false, text, emptyText: "No files here pass the filter." };
  }
  const scope = [included && `in ${included}`, excluded && `not ${excluded}`]
    .filter(Boolean)
    .join(", ");
  const quoted = `“${name}”`;
  return {
    active: true,
    search: true,
    text: scope ? `Names containing ${quoted} ${scope}` : `Names containing ${quoted}`,
    emptyText: scope ? `No files match ${quoted} ${scope}.` : `No files match ${quoted}.`,
  };
}
