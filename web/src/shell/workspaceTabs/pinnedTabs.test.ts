import { describe, expect, it } from "vitest";

import { isPinnedTab, withPinnedFirst } from "./pinnedTabs";
import type { WorkspaceTabRecord } from "./workspaceTab";

const tab = (id: string, pinned = false): WorkspaceTabRecord => ({
  id,
  place: { kind: "files", path: `${id}.ts` },
  openedBy: "user",
  ...(pinned ? { pinned: true as const } : {}),
});

describe("withPinnedFirst", () => {
  it("moves pinned tabs ahead, keeping each group's order", () => {
    const tabs = [tab("a"), tab("b", true), tab("c"), tab("d", true)];
    expect(withPinnedFirst(tabs).map((each) => each.id)).toEqual(["b", "d", "a", "c"]);
  });

  it("returns the same list when the order already holds, or nothing is pinned", () => {
    const ordered = [tab("a", true), tab("b"), tab("c")];
    expect(withPinnedFirst(ordered)).toBe(ordered);
    const none = [tab("a"), tab("b")];
    expect(withPinnedFirst(none)).toBe(none);
    const all = [tab("a", true), tab("b", true)];
    expect(withPinnedFirst(all)).toBe(all);
  });

  it("never pins a tab saved by a newer app, whatever it holds", () => {
    const opaque: WorkspaceTabRecord = { id: "u", unknown: { pinned: true } };
    expect(isPinnedTab(opaque)).toBe(false);
    const tabs = [opaque, tab("a", true)];
    expect(withPinnedFirst(tabs).map((each) => each.id)).toEqual(["a", "u"]);
  });
});
