// What a group header's right-click, ⋯ and ⌘K offer: Select All in Group where the list selects
// rows, then the page's own actions. Those on the group's rows wait while it's collapsed, so
// none acts on rows the user can't see.

import { ListChecksIcon, SquareDashedIcon } from "lucide-react";

import type { GroupAction } from "@/shell/usePullRequestListEntries";

export const EXPAND_GROUP_FIRST_REASON = "Expand the group first.";

/**
 * The header's actions, in order. `shownUrls` are its rows as shown, deduplicated: none
 * filtered out or folded into a collapsed stack.
 */
export function groupHeaderActions({
  title,
  collapsed,
  shownUrls,
  selectable,
  selection,
  pageActions,
  select,
}: {
  title: string;
  collapsed: boolean;
  shownUrls: readonly string[];
  selectable: boolean;
  selection: ReadonlySet<string>;
  pageActions: readonly GroupAction[];
  /** Checks, or with `checked` false unchecks, the group's rows as shown when it runs. */
  select: (checked: boolean) => void;
}): GroupAction[] {
  const actions: GroupAction[] = [];
  if (selectable && shownUrls.length > 0) {
    const allChecked = shownUrls.every((url) => selection.has(url));
    actions.push({
      id: "select-group",
      label: allChecked ? "Deselect All in Group" : "Select All in Group",
      paletteLabel: `${allChecked ? "Deselect" : "Select"} All Pull Requests in ${title} (${shownUrls.length})`,
      icon: allChecked ? SquareDashedIcon : ListChecksIcon,
      keywords: ["selection", "check", "group"],
      onRows: true,
      run: () => select(!allChecked),
    });
  }
  actions.push(...pageActions);
  return collapsed
    ? actions.map((action) =>
        action.onRows ? { ...action, unavailableReason: EXPAND_GROUP_FIRST_REASON } : action,
      )
    : actions;
}
