// A row's menu order: its own actions, then copy, then page-level ones, Unlink last.

import { LinkIcon } from "lucide-react";
import { describe, expect, it } from "vitest";

import type { RowAction } from "./PullRequestListRows";
import { withExtraRowActions } from "./rowActionOrder";

const action = (id: string, section: RowAction["section"]): RowAction => ({
  id: id as RowAction["id"],
  section,
  label: id,
  paletteLabel: id,
  icon: LinkIcon,
  run: () => {},
});

const ids = (actions: RowAction[]) => actions.map((entry) => entry.id);

describe("withExtraRowActions", () => {
  const own = [action("open", "open"), action("copy-link", "copy"), action("remove", "remove")];

  it("puts session actions before copy and page actions after it, Unlink last", () => {
    const placed = withExtraRowActions(own, [
      action("page:hide-repository", "page"),
      action("session:new", "session"),
    ]);
    expect(ids(placed)).toEqual([
      "open",
      "session:new",
      "copy-link",
      "page:hide-repository",
      "remove",
    ]);
  });

  it("ends with page actions where the row can't be unlinked", () => {
    const placed = withExtraRowActions(own.slice(0, 2), [action("page:hide-repository", "page")]);
    expect(ids(placed)).toEqual(["open", "copy-link", "page:hide-repository"]);
  });
});
