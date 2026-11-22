import { describe, expect, it, vi } from "vitest";

import { type TerminalLayout, closeTerminalPane } from "@/lib/paneSplitLayout";
import { focusSplitPane, insertSplitPane, nudgeSplitDivider } from "./splitTargetActions";
import { sidebarGroupTarget } from "./sidebarSplitTarget";
import {
  readTerminalGroups,
  TERMINAL_GROUPS_KEY,
  updateTerminalGroups,
} from "./terminalGroupsStore";
import { PANE_SPLITS_KEY, updatePaneSplit } from "./paneSplitStore";
import { mainSplitTarget, type TerminalSplitTarget } from "./terminalSplitTarget";
import { toggleZoomedPane, unzoomPane, zoomedPaneKey, zoomFocusedPane } from "./terminalPaneZoom";

const AGENT = "terminal:terminal_claude_main";
const SHELL = "terminal:terminal_zsh_u-1";
const OTHER = "terminal:terminal_zsh_u-2";
const SIDE_BY_SIDE: TerminalLayout = { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] };

let conversation = 0;

/** A main view split side by side, its shell pane focused. */
function mainSplit(): TerminalSplitTarget {
  const conversationId = `conv_zoom_${++conversation}`;
  updatePaneSplit(conversationId, () => ({ layout: SIDE_BY_SIDE, focusedKey: SHELL }));
  return mainSplitTarget(conversationId);
}

/** A sidebar group of two shells, the first focused. */
function sidebarSplit(): { target: TerminalSplitTarget; conversationId: string } {
  const conversationId = `conv_zoom_${++conversation}`;
  updateTerminalGroups(conversationId, () => [
    {
      id: "g_zoom",
      split: {
        layout: { axis: "columns", ratio: 0.5, children: [SHELL, OTHER] },
        focusedKey: SHELL,
      },
    },
  ]);
  return { target: sidebarGroupTarget(conversationId, "g_zoom"), conversationId };
}

describe("terminal pane zoom", () => {
  it("zooms the focused pane and toggles back", () => {
    const target = mainSplit();
    expect(toggleZoomedPane(target)).toBe(true);
    expect(zoomedPaneKey(target)).toBe(SHELL);
    expect(toggleZoomedPane(target)).toBe(false);
    expect(zoomedPaneKey(target)).toBeNull();
  });

  it("zooms nothing unsplit", () => {
    const target = mainSplitTarget(`conv_zoom_${++conversation}`);
    expect(zoomFocusedPane(target)).toBe(false);
    expect(zoomedPaneKey(target)).toBeNull();
  });

  it("unzooms when focus moves to another pane, and stays unzoomed on the way back", () => {
    const target = mainSplit();
    zoomFocusedPane(target);
    focusSplitPane(target, AGENT);
    expect(zoomedPaneKey(target)).toBeNull();
    focusSplitPane(target, SHELL);
    expect(zoomedPaneKey(target)).toBeNull();
  });

  it("unzooms on a new split, a close, and a divider move", () => {
    const split = mainSplit();
    zoomFocusedPane(split);
    insertSplitPane(split, {
      sourceKey: SHELL,
      newKey: OTHER,
      axis: "rows",
      unsplitKey: AGENT,
      takeFocus: false,
    });
    expect(zoomedPaneKey(split)).toBeNull();

    const closed = mainSplit();
    zoomFocusedPane(closed);
    // The other pane's shell exiting closes it, as the view's reconcile does.
    closed.update((current) => current && closeTerminalPane(current, AGENT));
    expect(zoomedPaneKey(closed)).toBeNull();

    const nudged = mainSplit();
    zoomFocusedPane(nudged);
    nudgeSplitDivider(nudged, SHELL, "left");
    expect(zoomedPaneKey(nudged)).toBeNull();
  });

  it("keeps a zoom per split: another session's and the sidebar's are their own", () => {
    const first = mainSplit();
    const second = mainSplit();
    const { target: sidebar } = sidebarSplit();
    zoomFocusedPane(first);
    zoomFocusedPane(sidebar);
    expect(zoomedPaneKey(second)).toBeNull();
    unzoomPane(first);
    expect(zoomedPaneKey(sidebar)).toBe(SHELL);
  });

  it("unzooms a sidebar group on a close, and never saves the zoom with the group", () => {
    const { target, conversationId } = sidebarSplit();
    zoomFocusedPane(target);
    focusSplitPane(target, OTHER);
    focusSplitPane(target, SHELL);
    expect(zoomedPaneKey(target)).toBeNull();

    const saved = window.localStorage.getItem(TERMINAL_GROUPS_KEY);
    zoomFocusedPane(target);
    expect(window.localStorage.getItem(TERMINAL_GROUPS_KEY)).toBe(saved);
    expect(Object.keys(readTerminalGroups(conversationId)[0]!.split)).toEqual([
      "layout",
      "focusedKey",
    ]);
    target.update((current) => current && closeTerminalPane(current, OTHER));
    expect(zoomedPaneKey(target)).toBeNull();
  });

  it("starts unzoomed after a reload, though the split is restored", async () => {
    const target = mainSplit();
    zoomFocusedPane(target);
    vi.resetModules();
    const reloaded = await import("./terminalPaneZoom");
    const { mainSplitTarget: reloadedTarget } = await import("./terminalSplitTarget");
    const restored = reloadedTarget(target.conversationId);
    expect(restored.read()?.focusedKey).toBe(SHELL);
    expect(reloaded.zoomedPaneKey(restored)).toBeNull();
  });

  describe("when another window changes saved splits", () => {
    /**
     * Another window saves under ``key``; its storage event, as this window gets it later.
     * Events queue, so storage may hold a later save by the time one is handled.
     */
    const otherWindowSaves = (key: string, save: () => void): StorageEvent => {
      const oldValue = window.localStorage.getItem(key);
      save();
      return new StorageEvent("storage", {
        key,
        oldValue,
        newValue: window.localStorage.getItem(key),
      });
    };
    const deliver = (...events: StorageEvent[]) => {
      for (const event of events) window.dispatchEvent(event);
    };
    /** Another window's save of ``conversationId``'s main split, focused on ``focusedKey``. */
    const mainFocus = (conversationId: string, focusedKey: string) =>
      otherWindowSaves(PANE_SPLITS_KEY, () => {
        const stored = JSON.parse(window.localStorage.getItem(PANE_SPLITS_KEY) ?? "{}") as {
          sessions?: Record<string, unknown>;
        };
        const sessions = {
          ...stored.sessions,
          [conversationId]: { layout: SIDE_BY_SIDE, focusedKey },
        };
        window.localStorage.setItem(PANE_SPLITS_KEY, JSON.stringify({ ...stored, sessions }));
      });
    interface GroupsJson {
      sessions: Record<string, { split: { focusedKey: string } }[]>;
    }
    /** Another window's edit of the saved sidebar groups. */
    const groupsEdit = (edit: (stored: GroupsJson) => void) =>
      otherWindowSaves(TERMINAL_GROUPS_KEY, () => {
        const stored = JSON.parse(
          window.localStorage.getItem(TERMINAL_GROUPS_KEY) ?? "{}",
        ) as GroupsJson;
        edit(stored);
        window.localStorage.setItem(TERMINAL_GROUPS_KEY, JSON.stringify(stored));
      });

    it("keeps the main view zoomed through another session's change", () => {
      const target = mainSplit();
      zoomFocusedPane(target);
      const other = mainSplit();
      deliver(mainFocus(other.conversationId, AGENT));
      expect(zoomedPaneKey(target)).toBe(SHELL);
    });

    it("unzooms the main view for its focus moving away and back, both saved before either event", () => {
      const target = mainSplit();
      zoomFocusedPane(target);
      const away = mainFocus(target.conversationId, AGENT);
      const back = mainFocus(target.conversationId, SHELL);
      deliver(away, back);
      expect(zoomedPaneKey(target)).toBeNull();
    });

    it("keeps a sidebar group zoomed through another session's change", () => {
      const { target } = sidebarSplit();
      zoomFocusedPane(target);
      const { conversationId: otherSession } = sidebarSplit();
      deliver(groupsEdit((stored) => Reflect.deleteProperty(stored.sessions, otherSession)));
      expect(zoomedPaneKey(target)).toBe(SHELL);
    });

    it("unzooms a sidebar group for its focus moving away and back, both saved before either event", () => {
      const { target, conversationId } = sidebarSplit();
      zoomFocusedPane(target);
      const focus = (key: string) =>
        groupsEdit((stored) => void (stored.sessions[conversationId]![0]!.split.focusedKey = key));
      const away = focus(OTHER);
      const back = focus(SHELL);
      deliver(away, back);
      expect(zoomedPaneKey(target)).toBeNull();
    });

    it("keeps this window's later save over another window's older change arriving after it", () => {
      const main = mainSplit();
      const olderMain = mainFocus(main.conversationId, AGENT);
      updatePaneSplit(main.conversationId, () => ({ layout: SIDE_BY_SIDE, focusedKey: SHELL }));
      deliver(olderMain);
      expect(main.read()?.focusedKey).toBe(SHELL);

      const { target, conversationId } = sidebarSplit();
      const olderGroups = groupsEdit(
        (stored) => void (stored.sessions[conversationId]![0]!.split.focusedKey = OTHER),
      );
      updateTerminalGroups(conversationId, (groups) =>
        groups.map((group) => ({ ...group, split: { ...group.split, focusedKey: SHELL } })),
      );
      deliver(olderGroups);
      expect(target.read()?.focusedKey).toBe(SHELL);
      expect(readTerminalGroups(conversationId)[0]!.split.focusedKey).toBe(SHELL);
    });

    it("unzooms everything when another window clears storage", () => {
      const main = mainSplit();
      const { target: sidebar } = sidebarSplit();
      zoomFocusedPane(main);
      zoomFocusedPane(sidebar);
      deliver(new StorageEvent("storage", { key: null }));
      expect(zoomedPaneKey(main)).toBeNull();
      expect(zoomedPaneKey(sidebar)).toBeNull();
    });
  });
});
