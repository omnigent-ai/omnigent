import { describe, expect, it } from "vitest";

import type { TerminalLayout, TerminalSplitState } from "@/lib/paneSplitLayout";

import { paneSwapDirections, paneSwapLabel, paneSwapNeighbor, swapSplitPane } from "./paneSwap";
import { zoomFocusedPane } from "./terminalPaneZoom";
import type { SplitTarget } from "./terminalSplitTarget";

// a on the left; b above c on the right.
const ABC: TerminalLayout = {
  axis: "columns",
  ratio: 0.3,
  children: ["a", { axis: "rows", ratio: 0.7, children: ["b", "c"] }],
};

let targets = 0;
/** A split kept in memory, as a session's store keeps one. */
function memoryTarget(initial: TerminalSplitState | undefined): SplitTarget & {
  state: () => TerminalSplitState | undefined;
} {
  let state = initial;
  return {
    id: `swap-test-${(targets += 1)}`,
    read: () => state,
    update: (update) => void (state = update(state)),
    state: () => state,
  };
}

describe("paneSwapLabel", () => {
  it("names each direction as the panes' shared table does", () => {
    expect(paneSwapLabel("left")).toBe("Swap with Pane Left");
    expect(paneSwapLabel("right")).toBe("Swap with Pane Right");
    expect(paneSwapLabel("up")).toBe("Swap with Pane Above");
    expect(paneSwapLabel("down")).toBe("Swap with Pane Below");
  });
});

describe("paneSwapDirections", () => {
  it("offers only the directions with a pane that way", () => {
    expect(paneSwapDirections(ABC, null, "a", 1)).toEqual(["right"]);
    expect(paneSwapDirections(ABC, null, "b", 1)).toEqual(["left", "down"]);
    expect(paneSwapDirections(ABC, null, "c", 1)).toEqual(["left", "up"]);
  });

  it("offers none for a lone pane, a missing one, or while a pane is maximized", () => {
    expect(paneSwapDirections("a", null, "a", 1)).toEqual([]);
    expect(paneSwapDirections(undefined, null, "a", 1)).toEqual([]);
    expect(paneSwapDirections(ABC, null, "gone", 1)).toEqual([]);
    expect(paneSwapDirections(ABC, "b", "b", 1)).toEqual([]);
    expect(paneSwapNeighbor(ABC, "a", "b", "left", 1)).toBeNull();
  });
});

describe("swapSplitPane", () => {
  it("trades places with the neighbour, keeps the dividers, and keeps the moved pane focused", () => {
    const target = memoryTarget({ layout: ABC, focusedKey: "c" });
    expect(swapSplitPane(target, "c", "left", 1)).toBe("a");
    expect(target.state()).toEqual({
      layout: {
        axis: "columns",
        ratio: 0.3,
        children: ["c", { axis: "rows", ratio: 0.7, children: ["b", "a"] }],
      },
      focusedKey: "c",
    });
  });

  it("swaps nothing at an edge", () => {
    const before: TerminalSplitState = { layout: ABC, focusedKey: "a" };
    const target = memoryTarget(before);
    expect(swapSplitPane(target, "a", "left", 1)).toBeNull();
    expect(target.state()).toBe(before);
  });

  it("swaps nothing while a pane is maximized", () => {
    const before: TerminalSplitState = { layout: ABC, focusedKey: "b" };
    const target = memoryTarget(before);
    expect(zoomFocusedPane(target)).toBe(true);
    expect(swapSplitPane(target, "b", "down", 1)).toBeNull();
    expect(target.state()).toBe(before);
  });
});
