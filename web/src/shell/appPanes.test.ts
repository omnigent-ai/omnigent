import { act, renderHook } from "@testing-library/react";
import { FolderTreeIcon } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  appPaneDropAt,
  appPaneKey,
  appPaneRoomRefusal,
  appPaneSidebarDrop,
  appPaneTabId,
  appPaneTabIds,
  dropTabAsPane,
  followReplacedAppPane,
  isAppPaneKey,
  openTabAsPane,
  resetAppPanes,
  setAppPaneTabActions,
  setAppPaneHost,
  setAppPaneTitle,
  setPendingAppPaneDrop,
  useAppPaneHosts,
  useAppPaneTitles,
} from "./appPanes";
import type { PaneSizing } from "./paneSizing";
import { registerSplitSizing } from "./splitTargetActions";
import { toggleZoomedPane, zoomedPaneKey } from "./terminalPaneZoom";
import { mainSplitTarget } from "./terminalSplitTarget";
import {
  type TerminalLayout,
  terminalLayoutGeometry,
  terminalLayoutKeys,
} from "@/lib/paneSplitLayout";

import { readPaneSplit, updatePaneSplit } from "./paneSplitStore";

const AGENT = "terminal:terminal_claude_main";
const SHELL = "terminal:terminal_zsh_a";

let conversation = 0;
const nextConversation = () => `conv_app_panes_${++conversation}`;

// jsdom has no elementFromPoint; the drop tests stub it, and each test starts without one.
const originalElementFromPoint = document.elementFromPoint;

const unregisterSizings: (() => void)[] = [];

afterEach(() => {
  for (const unregister of unregisterSizings.splice(0)) unregister();
  resetAppPanes();
  document.body.replaceChildren();
  document.elementFromPoint = originalElementFromPoint;
});

/** The session's panes laid out at 0,0 to 1000x500 client pixels. */
function placePanes(conversationId: string, size = { width: 1000, height: 500 }) {
  const root = document.createElement("div");
  root.dataset.splitPanes = conversationId;
  root.getBoundingClientRect = () => new DOMRect(0, 0, size.width, size.height);
  document.body.append(root);
}

/** A size rule for the session's panes that has room for at most two. */
function allowTwoPanes(conversationId: string) {
  const sizing: PaneSizing = {
    fits: (layout) => terminalLayoutKeys(layout).length <= 2,
    keeps: (_before, after) => terminalLayoutKeys(after).length <= 2,
    toward: (_target, from) => from,
    refusal: (axis) => `No room for another ${axis === "rows" ? "row" : "column"}.`,
  };
  unregisterSizings.push(registerSplitSizing(mainSplitTarget(conversationId).id, () => sizing));
}

describe("app pane keys", () => {
  it("names a workspace tab's pane apart from a terminal's", () => {
    expect(appPaneKey("tab-1")).toBe("app:tab-1");
    expect(appPaneTabId("app:tab-1")).toBe("tab-1");
    expect(appPaneTabId(SHELL)).toBeNull();
    expect(isAppPaneKey("app:tab-1")).toBe(true);
    expect(isAppPaneKey(AGENT)).toBe(false);
    expect(appPaneTabIds([AGENT, "app:a", SHELL, "app:b"])).toEqual(["a", "b"]);
  });
});

describe("openTabAsPane", () => {
  it("splits the agent's lone pane, with the tab taking focus", () => {
    const conversationId = nextConversation();
    expect(openTabAsPane(conversationId, "files", { agentKey: AGENT })).toBe(true);
    expect(readPaneSplit(conversationId)).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, "app:files"] },
      focusedKey: "app:files",
    });
  });

  it("goes beside the split's focused pane, and only focuses a tab already shown", () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    }));
    openTabAsPane(conversationId, "files", { agentKey: AGENT, axis: "rows" });
    const layout = readPaneSplit(conversationId)!.layout;
    expect(layout).toEqual({
      axis: "columns",
      ratio: 0.5,
      children: [AGENT, { axis: "rows", ratio: 0.5, children: [SHELL, "app:files"] }],
    });
    updatePaneSplit(conversationId, (state) => state && { ...state, focusedKey: AGENT });
    expect(openTabAsPane(conversationId, "files", { agentKey: AGENT })).toBe(true);
    expect(readPaneSplit(conversationId)).toEqual({ layout, focusedKey: "app:files" });
  });

  it("does nothing without a pane to put the tab beside", () => {
    const conversationId = nextConversation();
    expect(openTabAsPane(conversationId, "files", { agentKey: null })).toBe(false);
    expect(readPaneSplit(conversationId)).toBeUndefined();
  });
});

describe("app pane hosts and titles", () => {
  it("publishes each session's hosts and titles, and tells readers only of changes", () => {
    const conversationId = nextConversation();
    const hosts = renderHook(() => useAppPaneHosts(conversationId));
    const titles = renderHook(() => useAppPaneTitles(conversationId));
    const other = renderHook(() => useAppPaneHosts("conv_elsewhere"));
    const host = document.createElement("div");
    act(() => setAppPaneHost(conversationId, "files", host));
    expect(hosts.result.current.get("files")).toBe(host);
    expect(other.result.current.size).toBe(0);

    act(() => setAppPaneTitle(conversationId, "files", { label: "Files", icon: FolderTreeIcon }));
    const firstTitles = titles.result.current;
    expect(firstTitles.get("files")?.label).toBe("Files");
    // The same title again changes nothing, so a pane's row doesn't render again for it.
    act(() => setAppPaneTitle(conversationId, "files", { label: "Files", icon: FolderTreeIcon }));
    expect(titles.result.current).toBe(firstTitles);

    act(() => setAppPaneHost(conversationId, "files", null));
    expect(hosts.result.current.has("files")).toBe(false);
  });
});

describe("followReplacedAppPane", () => {
  it("keeps the pane when a New tab shown there becomes another tab", () => {
    const conversationId = nextConversation();
    openTabAsPane(conversationId, "new-1", { agentKey: AGENT });
    followReplacedAppPane(conversationId, "new-1", "shell-tab");
    expect(readPaneSplit(conversationId)).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, "app:shell-tab"] },
      focusedKey: "app:shell-tab",
    });
  });

  it("closes the New tab's pane and focuses the tab's own pane when it already has one", () => {
    const conversationId = nextConversation();
    openTabAsPane(conversationId, "files", { agentKey: AGENT });
    openTabAsPane(conversationId, "new-1", { agentKey: AGENT });
    followReplacedAppPane(conversationId, "new-1", "files");
    expect(readPaneSplit(conversationId)).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, "app:files"] },
      focusedKey: "app:files",
    });
  });

  it("shows a shell the New tab became as the split's own terminal pane, not an app pane", () => {
    const conversationId = nextConversation();
    openTabAsPane(conversationId, "new-1", { agentKey: AGENT });
    followReplacedAppPane(conversationId, "new-1", "shell-tab", SHELL);
    expect(readPaneSplit(conversationId)).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    });
  });

  it("keeps the pane and its focus when a New tab is filled in place, keeping its id", () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({
      layout: {
        axis: "columns",
        ratio: 0.5,
        children: [AGENT, { axis: "rows", ratio: 0.5, children: ["app:new-1", SHELL] }],
      },
      focusedKey: "app:new-1",
    }));
    const before = readPaneSplit(conversationId);
    followReplacedAppPane(conversationId, "new-1", "new-1");
    expect(readPaneSplit(conversationId)).toEqual(before);
  });

  it("leaves the split alone for a tab that isn't a pane", () => {
    const conversationId = nextConversation();
    openTabAsPane(conversationId, "files", { agentKey: AGENT });
    const before = readPaneSplit(conversationId);
    followReplacedAppPane(conversationId, "new-1", "diffs");
    expect(readPaneSplit(conversationId)).toEqual(before);
  });
});

describe("dropping a sidebar tab on the panes", () => {
  it("finds the pane under the pointer and the side nearest it", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    expect(appPaneDropAt(conversationId, { x: 980, y: 250 }, AGENT)).toEqual({
      targetKey: AGENT,
      side: "right",
      area: { left: 50, top: 0, width: 50, height: 100 },
    });
    expect(appPaneDropAt(conversationId, { x: 500, y: 490 }, AGENT)?.side).toBe("bottom");
    expect(appPaneDropAt(conversationId, { x: 1200, y: 250 }, AGENT)).toBeNull();
    expect(appPaneDropAt(conversationId, { x: 500, y: 250 }, null)).toBeNull();
  });

  it("offers Bottom below a tall pane's middle, and keeps the side shown near a diagonal", () => {
    const conversationId = nextConversation();
    // The shell is the right column: 500 wide and 800 tall.
    placePanes(conversationId, { width: 1000, height: 800 });
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: AGENT,
    }));
    const bottom = appPaneDropAt(conversationId, { x: 750, y: 480 }, AGENT);
    expect(bottom).toEqual({
      targetKey: SHELL,
      side: "bottom",
      area: { left: 50, top: 50, width: 50, height: 50 },
    });
    // Just past the diagonal toward the right: the preview's Bottom stays.
    expect(appPaneDropAt(conversationId, { x: 850, y: 544 }, AGENT)?.side).toBe("right");
    setPendingAppPaneDrop(conversationId, bottom);
    expect(appPaneDropAt(conversationId, { x: 850, y: 544 }, AGENT)?.side).toBe("bottom");
    setPendingAppPaneDrop(conversationId, null);
  });

  it("previews where a tab dropped beside a split's pane lands, as the split keeps proportions", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: AGENT,
    }));
    // Right of the right pane: the row grows to thirds, so the tab takes the last third.
    const drop = appPaneDropAt(conversationId, { x: 980, y: 250 }, AGENT);
    expect(drop?.targetKey).toBe(SHELL);
    expect(drop?.side).toBe("right");
    expect(drop?.area.left).toBeCloseTo(200 / 3);
    expect(drop?.area.width).toBeCloseTo(100 / 3);
    expect(dropTabAsPane(conversationId, "files", drop!, AGENT)).toBe(true);
    const landed = terminalLayoutGeometry(readPaneSplit(conversationId)!.layout).panes.find(
      (pane) => pane.key === "app:files",
    );
    expect(landed?.left).toBeCloseTo(drop!.area.left);
    expect(landed?.width).toBeCloseTo(drop!.area.width);
    expect(landed?.top).toBe(drop!.area.top);
    expect(landed?.height).toBe(drop!.area.height);
  });

  it("offers no spot while a pane is maximized", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    }));
    toggleZoomedPane(mainSplitTarget(conversationId));
    expect(appPaneDropAt(conversationId, { x: 900, y: 250 }, AGENT)).toBeNull();
  });

  it("refuses a drop placed before a pane was maximized, changing nothing", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    }));
    const drop = appPaneDropAt(conversationId, { x: 980, y: 250 }, AGENT);
    expect(drop).not.toBeNull();
    const target = mainSplitTarget(conversationId);
    toggleZoomedPane(target);
    const before = readPaneSplit(conversationId);
    expect(dropTabAsPane(conversationId, "files", drop!, AGENT)).toBe(false);
    expect(readPaneSplit(conversationId)).toEqual(before);
    expect(zoomedPaneKey(target)).toBe(SHELL);
  });

  it("opens the tab on the side it met, taking focus, once", () => {
    const conversationId = nextConversation();
    expect(dropTabAsPane(conversationId, "files", { targetKey: AGENT, side: "left" }, AGENT)).toBe(
      true,
    );
    expect(readPaneSplit(conversationId)).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: ["app:files", AGENT] },
      focusedKey: "app:files",
    });
    expect(
      dropTabAsPane(conversationId, "files", { targetKey: AGENT, side: "bottom" }, AGENT),
    ).toBe(false);
  });
});

describe("dragging an app pane onto the workspace sidebar", () => {
  function placeSidebar() {
    const sidebar = document.createElement("aside");
    sidebar.setAttribute("aria-label", "Workspace");
    document.body.append(sidebar);
    return sidebar;
  }

  it("marks the sidebar while over it and moves the tab back on a drop", () => {
    const conversationId = nextConversation();
    const sidebar = placeSidebar();
    const moveToSidebar = vi.fn();
    setAppPaneTabActions(conversationId, { close: vi.fn(), moveToSidebar });
    const elementFromPoint = vi.fn<(x: number, y: number) => Element | null>(() => sidebar);
    document.elementFromPoint = elementFromPoint;
    const drop = appPaneSidebarDrop(conversationId);

    drop.over("app:files", { x: 10, y: 10 });
    expect(sidebar.hasAttribute("data-app-pane-drop-target")).toBe(true);
    drop.over("app:files", null);
    expect(sidebar.hasAttribute("data-app-pane-drop-target")).toBe(false);

    expect(drop.drop("app:files", { x: 10, y: 10 })).toBe(true);
    expect(moveToSidebar).toHaveBeenCalledWith("files");
  });

  it("ignores a terminal's pane and a drop elsewhere", () => {
    const conversationId = nextConversation();
    const sidebar = placeSidebar();
    const moveToSidebar = vi.fn();
    setAppPaneTabActions(conversationId, { close: vi.fn(), moveToSidebar });
    document.elementFromPoint = () => sidebar;
    const drop = appPaneSidebarDrop(conversationId);
    drop.over(SHELL, { x: 10, y: 10 });
    expect(sidebar.hasAttribute("data-app-pane-drop-target")).toBe(false);
    expect(drop.drop(SHELL, { x: 10, y: 10 })).toBe(false);

    document.elementFromPoint = () => document.body;
    expect(drop.drop("app:files", { x: 10, y: 10 })).toBe(false);
    expect(moveToSidebar).not.toHaveBeenCalled();
  });

  it("takes a shell pane its mover may move, and moves it on a drop over the sidebar", () => {
    const conversationId = nextConversation();
    const sidebar = placeSidebar();
    const movable = new Set([SHELL]);
    const mover = { canMove: (key: string) => movable.has(key), move: vi.fn(() => true) };
    document.elementFromPoint = () => sidebar;
    const drop = appPaneSidebarDrop(conversationId, mover);

    drop.over(AGENT, { x: 10, y: 10 });
    expect(sidebar.hasAttribute("data-app-pane-drop-target")).toBe(false);
    expect(drop.drop(AGENT, { x: 10, y: 10 })).toBe(false);
    drop.over(SHELL, { x: 10, y: 10 });
    expect(sidebar.hasAttribute("data-app-pane-drop-target")).toBe(true);
    expect(drop.drop(SHELL, { x: 10, y: 10 })).toBe(true);
    expect(mover.move).toHaveBeenCalledExactlyOnceWith(SHELL);
    expect(sidebar.hasAttribute("data-app-pane-drop-target")).toBe(false);

    // Dropped anywhere but the sidebar, the shell stays where it is.
    document.elementFromPoint = () => document.body;
    expect(drop.drop(SHELL, { x: 10, y: 10 })).toBe(false);
    expect(mover.move).toHaveBeenCalledOnce();
  });
});

describe("a size rule with no room for another pane", () => {
  const SPLIT: TerminalLayout = { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] };

  it("keeps a tab from opening as a pane, and says why", () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({ layout: SPLIT, focusedKey: SHELL }));
    allowTwoPanes(conversationId);
    const before = readPaneSplit(conversationId);

    expect(openTabAsPane(conversationId, "files", { agentKey: AGENT })).toBe(false);
    expect(readPaneSplit(conversationId)).toEqual(before);
    expect(appPaneRoomRefusal(conversationId, "files", { agentKey: AGENT })).toBe(
      "No room for another column.",
    );
    expect(appPaneRoomRefusal(conversationId, null, { agentKey: AGENT, axis: "rows" })).toBe(
      "No room for another row.",
    );
  });

  it("still opens a tab beside the agent's lone pane while the rule has room", () => {
    const conversationId = nextConversation();
    allowTwoPanes(conversationId);
    expect(appPaneRoomRefusal(conversationId, "files", { agentKey: AGENT })).toBeUndefined();
    expect(openTabAsPane(conversationId, "files", { agentKey: AGENT })).toBe(true);
    // A tab the split already shows needs no room, now that the rule has none: it only takes focus.
    updatePaneSplit(conversationId, (state) => state && { ...state, focusedKey: AGENT });
    const { layout } = readPaneSplit(conversationId)!;
    expect(appPaneRoomRefusal(conversationId, "files", { agentKey: AGENT })).toBeUndefined();
    expect(openTabAsPane(conversationId, "files", { agentKey: AGENT })).toBe(true);
    expect(readPaneSplit(conversationId)).toEqual({ layout, focusedKey: "app:files" });
  });

  it("offers no drop spot, and refuses a drop the preview offered before it ran out", () => {
    const conversationId = nextConversation();
    placePanes(conversationId);
    updatePaneSplit(conversationId, () => ({ layout: SPLIT, focusedKey: SHELL }));
    const drop = appPaneDropAt(conversationId, { x: 980, y: 250 }, AGENT);
    expect(drop).not.toBeNull();
    // Both spots are offered while there's room, so the rule is what refuses them below.
    expect(appPaneDropAt(conversationId, { x: 250, y: 490 }, AGENT)?.side).toBe("bottom");

    allowTwoPanes(conversationId);
    expect(appPaneDropAt(conversationId, { x: 980, y: 250 }, AGENT)).toBeNull();
    expect(appPaneDropAt(conversationId, { x: 250, y: 490 }, AGENT)).toBeNull();
    const before = readPaneSplit(conversationId);
    expect(dropTabAsPane(conversationId, "files", drop!, AGENT)).toBe(false);
    expect(readPaneSplit(conversationId)).toEqual(before);
  });
});
