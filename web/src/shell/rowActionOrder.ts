// Where a caller's row actions go in a row's menus and ⌘K: actions on the PR itself before
// its copy actions, and `page` ones, which act beyond the PR, after them and before Unlink,
// so removing it stays last.

import type { RowAction } from "./PullRequestListRows";

export function withExtraRowActions(
  actions: readonly RowAction[],
  extra: readonly RowAction[],
): RowAction[] {
  const placed = [...actions];
  const own = extra.filter((action) => action.section !== "page");
  const firstCopy = placed.findIndex((action) => action.section === "copy");
  placed.splice(firstCopy < 0 ? placed.length : firstCopy, 0, ...own);
  const page = extra.filter((action) => action.section === "page");
  const firstRemove = placed.findIndex((action) => action.section === "remove");
  placed.splice(firstRemove < 0 ? placed.length : firstRemove, 0, ...page);
  return placed;
}
