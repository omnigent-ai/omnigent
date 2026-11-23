import { describe, expect, it } from "vitest";

import {
  terminalLayoutKeys,
  type TerminalLayout,
  type TerminalSplitState,
} from "@/lib/paneSplitLayout";

import type { PaneSizing } from "./paneSizing";
import {
  equalizeSplit,
  focusSplitPane,
  insertSplitPane,
  measureSplitSizing,
  moveSplitFocus,
  nudgeSplitDivider,
  registerSplitSizing,
  removeSplitPane,
  roomToSplit,
} from "./splitTargetActions";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

let nextTarget = 0;

/** A split kept in memory, starting from `initial`. */
function memoryTarget(initial?: TerminalSplitState) {
  let state = initial;
  const target: TerminalSplitTarget = {
    id: `target-${nextTarget++}`,
    conversationId: "session",
    read: () => state,
    update: (update) => {
      state = update(state);
    },
    unsplitKey: () => "agent",
    canClosePane: (key) => key !== "agent",
    focusSurface: "main",
  };
  return { target, read: () => state };
}

/** A rule that lets at most `limit` panes share the view. */
const atMost = (limit: number): PaneSizing => ({
  fits: (layout) => terminalLayoutKeys(layout).length <= limit,
  keeps: (_before, after) => terminalLayoutKeys(after).length <= limit,
  toward: (_target, from) => from,
  refusal: () => "No room.",
});

const SIDE_BY_SIDE: TerminalLayout = { axis: "columns", ratio: 0.5, children: ["agent", "shell"] };

describe("splitTargetActions", () => {
  it("splits the unsplit view from its lone pane, and the new pane takes focus when asked", () => {
    const { target, read } = memoryTarget();
    expect(
      insertSplitPane(target, {
        sourceKey: "agent",
        newKey: "shell",
        axis: "columns",
        unsplitKey: "agent",
        takeFocus: true,
      }),
    ).toBe(true);
    expect(read()).toEqual({ layout: SIDE_BY_SIDE, focusedKey: "shell" });
  });

  it.each([
    ["the source pane is gone", "gone", "new"],
    ["the new pane is already shown", "agent", "shell"],
    ["the new pane is the one it splits", "shell", "shell"],
  ])("splits nothing once %s", (_case, sourceKey, newKey) => {
    const { target, read } = memoryTarget({ layout: SIDE_BY_SIDE, focusedKey: "agent" });
    const inserted = insertSplitPane(target, {
      sourceKey,
      newKey,
      axis: "rows",
      unsplitKey: "agent",
      takeFocus: true,
    });
    expect(inserted).toBe(false);
    expect(read()?.layout).toBe(SIDE_BY_SIDE);
  });

  it("checks a split against the sizing its host registered, and allows any while unmeasured", () => {
    const { target } = memoryTarget({ layout: SIDE_BY_SIDE, focusedKey: "agent" });
    expect(measureSplitSizing(target)).toBeNull();
    expect(roomToSplit(target, "agent", "columns", "after", "agent")).toBe(true);
    const unregister = registerSplitSizing(target.id, () => atMost(2));
    expect(roomToSplit(target, "agent", "columns", "after", "agent")).toBe(false);
    unregister();
    expect(roomToSplit(target, "agent", "columns", "after", "agent")).toBe(true);
  });

  it("keeps a later host's sizing when an earlier one unregisters", () => {
    const { target } = memoryTarget();
    const first = registerSplitSizing(target.id, () => atMost(1));
    registerSplitSizing(target.id, () => atMost(3));
    first();
    expect(measureSplitSizing(target)?.fits(SIDE_BY_SIDE)).toBe(true);
  });

  it("moves focus to the pane beside it, and stays put at the edge", () => {
    const { target, read } = memoryTarget({ layout: SIDE_BY_SIDE, focusedKey: "agent" });
    expect(moveSplitFocus(target, "agent", "right", 1)).toBe("shell");
    expect(read()?.focusedKey).toBe("shell");
    expect(moveSplitFocus(target, "shell", "right", 1)).toBeNull();
    focusSplitPane(target, "missing");
    expect(read()?.focusedKey).toBe("shell");
  });

  it("nudges a divider unless the sizing refuses the result", () => {
    const { target, read } = memoryTarget({ layout: SIDE_BY_SIDE, focusedKey: "agent" });
    nudgeSplitDivider(target, "agent", "right");
    expect(read()?.layout).toMatchObject({ ratio: 0.53 });
    const refuse: PaneSizing = { ...atMost(2), keeps: () => false };
    const before = read()?.layout;
    nudgeSplitDivider(target, "agent", "right", refuse);
    expect(read()?.layout).toBe(before);
  });

  it("equalizes the panes, or goes as near as the sizing allows", () => {
    const uneven: TerminalLayout = { axis: "columns", ratio: 0.3, children: ["agent", "shell"] };
    const { target, read } = memoryTarget({ layout: uneven, focusedKey: "agent" });
    const stuck: PaneSizing = { ...atMost(2), keeps: () => false };
    equalizeSplit(target, stuck);
    expect(read()?.layout).toBe(uneven);
    equalizeSplit(target);
    expect(read()?.layout).toMatchObject({ ratio: 0.5 });
  });

  it("goes back to unsplit when the last but one pane closes", () => {
    const { target, read } = memoryTarget({ layout: SIDE_BY_SIDE, focusedKey: "shell" });
    removeSplitPane(target, "shell");
    expect(read()).toBeUndefined();
  });
});
