// What the Files tree's filters do right now, in words. Include and exclude globs
// narrow a name search and do nothing alone, so only a name makes a filter.

export interface TreeFilterValues {
  nameContains: string;
  include: string;
  exclude: string;
}

export type TreeFilterSummary =
  /** No filter is narrowing the tree. */
  | { active: false; inertGlobs: boolean }
  /** The tree shows a name search; `text` describes it. */
  | { active: true; text: string; emptyText: string };

export function treeFilterSummary({
  nameContains,
  include,
  exclude,
}: TreeFilterValues): TreeFilterSummary {
  const name = nameContains.trim();
  const included = include.trim();
  const excluded = exclude.trim();
  if (!name) return { active: false, inertGlobs: Boolean(included || excluded) };
  const scope = [included && `in ${included}`, excluded && `not ${excluded}`]
    .filter(Boolean)
    .join(", ");
  const quoted = `“${name}”`;
  return {
    active: true,
    text: scope ? `Names containing ${quoted} ${scope}` : `Names containing ${quoted}`,
    emptyText: scope ? `No files match ${quoted} ${scope}.` : `No files match ${quoted}.`,
  };
}
