// A group header's actions: Select All in Group where the list selects rows, the page's own
// after it, and those on the group's rows held while it's collapsed.

import { EyeOffIcon, GitPullRequestDraftIcon } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import type { GroupAction } from "@/shell/usePullRequestListEntries";

import { EXPAND_GROUP_FIRST_REASON, groupHeaderActions } from "./groupHeaderActions";

const hide: GroupAction = { id: "hide", label: "Hide Repository", icon: EyeOffIcon, run: vi.fn() };
const ready: GroupAction = {
  id: "ready",
  label: "Mark All Ready for Review",
  icon: GitPullRequestDraftIcon,
  onRows: true,
  run: vi.fn(),
};

function actionsOf(over: Partial<Parameters<typeof groupHeaderActions>[0]> = {}) {
  const select = vi.fn();
  const actions = groupHeaderActions({
    title: "Draft",
    collapsed: false,
    shownUrls: ["a", "b"],
    selectable: true,
    selection: new Set(),
    pageActions: [ready, hide],
    select,
    ...over,
  });
  return { actions, select };
}

describe("a group header's actions", () => {
  it("selects the group's rows first, then offers the page's", () => {
    const { actions, select } = actionsOf({ selection: new Set(["a", "elsewhere"]) });
    expect(actions.map((action) => [action.label, action.paletteLabel])).toEqual([
      ["Select All in Group", "Select All Pull Requests in Draft (2)"],
      ["Mark All Ready for Review", undefined],
      ["Hide Repository", undefined],
    ]);
    actions[0]!.run();
    expect(select).toHaveBeenCalledWith(true);
  });

  it("deselects them once every one shown is checked", () => {
    const { actions, select } = actionsOf({ selection: new Set(["a", "b"]) });
    expect(actions[0]).toMatchObject({
      label: "Deselect All in Group",
      paletteLabel: "Deselect All Pull Requests in Draft (2)",
    });
    actions[0]!.run();
    expect(select).toHaveBeenCalledWith(false);
  });

  it("offers no selection where the list selects none or the group shows none", () => {
    expect(actionsOf({ selectable: false }).actions.map((action) => action.id)).toEqual([
      "ready",
      "hide",
    ]);
    expect(actionsOf({ shownUrls: [] }).actions.map((action) => action.id)).toEqual([
      "ready",
      "hide",
    ]);
  });

  it("holds what acts on its rows while it's collapsed, and says why", () => {
    const { actions } = actionsOf({ collapsed: true });
    expect(actions.map((action) => [action.id, action.unavailableReason])).toEqual([
      ["select-group", EXPAND_GROUP_FIRST_REASON],
      ["ready", EXPAND_GROUP_FIRST_REASON],
      ["hide", undefined],
    ]);
  });
});
