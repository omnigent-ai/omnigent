import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TerminalSplitState } from "@/lib/paneSplitLayout";

import { giveNextPaneTheKeyboard, removePaneFocusingNeighbor } from "./paneShellClose";
import {
  isFailedPaneStart,
  recordPaneStartFailure,
  resetPaneStartFailuresForTests,
} from "./paneStartFailures";
import { pendingTerminalFocus, settleTerminalFocus } from "./terminalFocusRequest";
import { resetTerminalOpenRequestsForTests, useTerminalOpenRequests } from "./terminalOpenRequest";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

const LIVE = "terminal:terminal_zsh_a";
const OTHER = "terminal:terminal_zsh_b";
const FAILED = "terminal:terminal_zsh_failed";

let conversation = 0;

/** A sidebar group's split held in memory, with the session's shell opener listening. */
function sidebarSplit(state: TerminalSplitState) {
  const conversationId = `conv_pane_close_${++conversation}`;
  let current: TerminalSplitState | undefined = state;
  const target: TerminalSplitTarget = {
    id: `${conversationId}|sidebar|g_test`,
    conversationId,
    read: () => current,
    update: (update) => {
      current = update(current);
    },
    unsplitKey: () => null,
    canClosePane: () => true,
    focusSurface: "rail",
  };
  const opened = vi.fn();
  renderHook(() => useTerminalOpenRequests(conversationId, opened));
  return { target, conversationId, opened };
}

afterEach(() => {
  resetPaneStartFailuresForTests();
  resetTerminalOpenRequestsForTests();
});

describe("removePaneFocusingNeighbor", () => {
  it("opens the shell beside a closed pane in the sidebar and gives it the keyboard", () => {
    const { target, conversationId, opened } = sidebarSplit({
      layout: { axis: "columns", ratio: 0.5, children: [LIVE, OTHER] },
      focusedKey: LIVE,
    });
    removePaneFocusingNeighbor(target, LIVE);
    expect(target.read()).toBeUndefined();
    expect(opened).toHaveBeenCalledExactlyOnceWith({ key: OTHER, focus: true });
    expect(pendingTerminalFocus(conversationId, "rail")?.tabKey).toBe(OTHER);
    settleTerminalFocus(conversationId, "rail");
  });

  it("ends the split without opening a failed start when its last live shell closes", () => {
    const { target, conversationId, opened } = sidebarSplit({
      layout: { axis: "columns", ratio: 0.5, children: [LIVE, FAILED] },
      focusedKey: LIVE,
    });
    recordPaneStartFailure(conversationId, FAILED, { terminal: "zsh", reason: "No runner." });
    removePaneFocusingNeighbor(target, LIVE);
    expect(target.read()).toBeUndefined();
    // The failed start went with its split, and has no tab to open.
    expect(isFailedPaneStart(conversationId, FAILED)).toBe(false);
    expect(opened).not.toHaveBeenCalled();
    expect(pendingTerminalFocus(conversationId, "rail")).toBeNull();
  });
});

describe("giveNextPaneTheKeyboard", () => {
  it("opens the given pane's shell in the sidebar and gives it the keyboard", () => {
    const { target, conversationId, opened } = sidebarSplit({
      layout: { axis: "columns", ratio: 0.5, children: [LIVE, OTHER] },
      focusedKey: LIVE,
    });
    giveNextPaneTheKeyboard(target, OTHER);
    expect(opened).toHaveBeenCalledExactlyOnceWith({ key: OTHER, focus: true });
    expect(pendingTerminalFocus(conversationId, "rail")?.tabKey).toBe(OTHER);
    settleTerminalFocus(conversationId, "rail");
  });

  it("leaves a failed start's slot to take the keyboard on its own Try Again", () => {
    const { target, conversationId, opened } = sidebarSplit({
      layout: { axis: "columns", ratio: 0.5, children: [LIVE, FAILED] },
      focusedKey: LIVE,
    });
    recordPaneStartFailure(conversationId, FAILED, { terminal: "zsh", reason: "No runner." });
    giveNextPaneTheKeyboard(target, FAILED);
    // The split is shown through its live shell, without moving the keyboard there.
    expect(opened).toHaveBeenCalledExactlyOnceWith({ key: LIVE, focus: false });
    expect(pendingTerminalFocus(conversationId, "rail")).toBeNull();
  });

  it("does nothing when no pane is left to take the keyboard", () => {
    const { target, conversationId, opened } = sidebarSplit({
      layout: { axis: "columns", ratio: 0.5, children: [LIVE, OTHER] },
      focusedKey: LIVE,
    });
    giveNextPaneTheKeyboard(target, null);
    expect(opened).not.toHaveBeenCalled();
    expect(pendingTerminalFocus(conversationId, "rail")).toBeNull();
  });
});
