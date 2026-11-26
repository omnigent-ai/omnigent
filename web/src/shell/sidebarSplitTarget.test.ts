import { afterEach, describe, expect, it } from "vitest";

import { groupKeys } from "@/lib/terminalGroups";
import { splitTerminalLayout } from "@/lib/paneSplitLayout";

import { focusSplitPane, insertSplitPane, removeSplitPane } from "./splitTargetActions";
import {
  beginClosingShells,
  readTerminalGroups,
  resetTerminalGroupsCache,
  TERMINAL_GROUPS_KEY,
} from "./terminalGroupsStore";
import { sidebarGroupTarget, sidebarShellTarget, sidebarTargetFor } from "./sidebarSplitTarget";

let session = 0;
const nextSession = () => `conv_sidebar_${++session}`;

afterEach(() => {
  window.localStorage.clear();
  resetTerminalGroupsCache();
});

describe("sidebar split targets", () => {
  it("makes a group on a shell's first split, then splits that group", () => {
    const id = nextSession();
    const lone = sidebarShellTarget(id, "terminal:a");
    expect(lone.read()).toBeUndefined();
    expect(
      insertSplitPane(lone, {
        sourceKey: "terminal:a",
        newKey: "terminal:b",
        axis: "columns",
        unsplitKey: lone.unsplitKey(null),
        takeFocus: true,
      }),
    ).toBe(true);
    const [group] = readTerminalGroups(id);
    expect(groupKeys(group)).toEqual(["terminal:a", "terminal:b"]);
    expect(group.split.focusedKey).toBe("terminal:b");
    // The tab of either shell now acts on that group.
    const groupTarget = sidebarTargetFor(id, "terminal:b");
    expect(groupTarget.id).toBe(sidebarGroupTarget(id, group.id).id);

    expect(
      insertSplitPane(groupTarget, {
        sourceKey: "terminal:b",
        newKey: "terminal:c",
        axis: "rows",
        unsplitKey: groupTarget.unsplitKey(null),
        takeFocus: true,
      }),
    ).toBe(true);
    const groups = readTerminalGroups(id);
    expect(groups).toHaveLength(1);
    expect(groups[0].id).toBe(group.id);
    expect(groupKeys(groups[0])).toEqual(["terminal:a", "terminal:b", "terminal:c"]);
  });

  it("drops a group once closing leaves it one pane, so that shell is ordinary again", () => {
    const id = nextSession();
    const lone = sidebarShellTarget(id, "terminal:a");
    lone.update(() => ({
      layout: splitTerminalLayout("terminal:a", "terminal:a", "terminal:b", "columns"),
      focusedKey: "terminal:a",
    }));
    const [group] = readTerminalGroups(id);
    removeSplitPane(sidebarGroupTarget(id, group.id), "terminal:b");
    expect(readTerminalGroups(id)).toEqual([]);
    expect(sidebarTargetFor(id, "terminal:a").unsplitKey(null)).toBe("terminal:a");
  });

  it("keeps fields a newer version saved on a group and its split as focus moves", () => {
    const id = nextSession();
    const layout = { axis: "columns", ratio: 0.5, children: ["terminal:a", "terminal:b"] };
    window.localStorage.setItem(
      TERMINAL_GROUPS_KEY,
      JSON.stringify({
        sessions: {
          [id]: [
            {
              id: "g_ab",
              futureField: "kept",
              split: { layout, focusedKey: "terminal:b", splitExtra: 1 },
            },
          ],
        },
      }),
    );
    resetTerminalGroupsCache();
    focusSplitPane(sidebarGroupTarget(id, "g_ab"), "terminal:a");
    const [saved] = JSON.parse(window.localStorage.getItem(TERMINAL_GROUPS_KEY)!).sessions[id];
    expect(saved).toEqual({
      id: "g_ab",
      futureField: "kept",
      split: { layout, focusedKey: "terminal:a", splitExtra: 1 },
    });
  });

  it("takes no split into a group whose shells are closing, so the new shell stays its own tab", () => {
    const id = nextSession();
    const lone = sidebarShellTarget(id, "terminal:a");
    lone.update(() => ({
      layout: splitTerminalLayout("terminal:a", "terminal:a", "terminal:b", "columns"),
      focusedKey: "terminal:a",
    }));
    const [group] = readTerminalGroups(id);
    const release = beginClosingShells(id, groupKeys(group));
    expect(release).not.toBeNull();
    expect(beginClosingShells(id, ["terminal:b"])).toBeNull();
    const insert = () =>
      insertSplitPane(sidebarTargetFor(id, "terminal:a"), {
        sourceKey: "terminal:a",
        newKey: "terminal:c",
        axis: "rows",
        unsplitKey: null,
        takeFocus: true,
      });
    expect(insert()).toBe(false);
    expect(readTerminalGroups(id)).toEqual([group]);
    release!();
    expect(insert()).toBe(true);
  });

  it("lets any shell pane close, and changes nothing for a group that's gone", () => {
    const id = nextSession();
    const gone = sidebarGroupTarget(id, "g_gone");
    expect(gone.canClosePane("terminal:a")).toBe(true);
    gone.update(() => undefined);
    expect(readTerminalGroups(id)).toEqual([]);
    expect(
      insertSplitPane(gone, {
        sourceKey: "terminal:a",
        newKey: "terminal:b",
        axis: "rows",
        unsplitKey: gone.unsplitKey(null),
        takeFocus: true,
      }),
    ).toBe(false);
  });
});
