import { describe, expect, it } from "vitest";
import {
  parseTerminalSplitState,
  clampSplitRatio,
  reconcileTerminalSplit,
  removeTerminalLayoutKey,
  replaceTerminalLayoutKey,
  resizeTerminalSplit,
  splitTerminalLayout,
  terminalLayoutGeometry,
  terminalLayoutKeys,
  type TerminalLayout,
} from "./terminalSplitLayout";

const twoByOne: TerminalLayout = {
  axis: "columns",
  ratio: 0.5,
  children: ["agent", { axis: "rows", ratio: 0.5, children: ["a", "b"] }],
};

describe("terminalSplitLayout", () => {
  it("splits only the target pane, nesting the new key after it", () => {
    expect(splitTerminalLayout("agent", "agent", "a", "columns")).toEqual({
      axis: "columns",
      ratio: 0.5,
      children: ["agent", "a"],
    });
    const nested = splitTerminalLayout(twoByOne, "b", "c", "columns");
    expect(terminalLayoutKeys(nested)).toEqual(["agent", "a", "b", "c"]);
    expect(splitTerminalLayout(twoByOne, "missing", "c", "rows")).toEqual(twoByOne);
  });

  it("gives a removed pane's space to its sibling", () => {
    expect(removeTerminalLayoutKey(twoByOne, "a")).toEqual({
      axis: "columns",
      ratio: 0.5,
      children: ["agent", "b"],
    });
    expect(removeTerminalLayoutKey("agent", "agent")).toBeNull();
    expect(removeTerminalLayoutKey(twoByOne, "missing")).toBe(twoByOne);
  });

  it("clamps resized splits to a quarter..three quarters of the parent", () => {
    const wide = resizeTerminalSplit(twoByOne, [], 0.95) as Exclude<TerminalLayout, string>;
    expect(wide.ratio).toBe(0.75);
    const tall = resizeTerminalSplit(twoByOne, [1], 0.1) as Exclude<TerminalLayout, string>;
    expect((tall.children[1] as Exclude<TerminalLayout, string>).ratio).toBe(0.25);
    expect(clampSplitRatio(Number.NaN)).toBe(0.5);
  });

  it("reconciles missing panes only when they are known gone", () => {
    const state = { layout: twoByOne, focusedKey: "b" };
    const live = new Set(["agent", "a"]);
    const options = (removable: boolean, liveAgentKey: string | null = "agent") => ({
      liveAgentKey,
      isAgentKey: (key: string) => key === "agent" || key === "codex",
      removable: () => removable,
    });

    expect(reconcileTerminalSplit(state, live, options(false))).toBe(state);
    expect(reconcileTerminalSplit(state, live, options(true))).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: ["agent", "a"] },
      focusedKey: "agent",
    });
    expect(
      reconcileTerminalSplit(state, new Set(["codex", "a", "b"]), options(false, "codex")),
    ).toMatchObject({ focusedKey: "b" });
    expect(
      reconcileTerminalSplit(
        { layout: twoByOne, focusedKey: "a" },
        new Set(["agent"]),
        options(true),
      ),
    ).toBeUndefined();
  });

  it("swaps one pane key in place", () => {
    expect(terminalLayoutKeys(replaceTerminalLayoutKey(twoByOne, "agent", "codex"))).toEqual([
      "codex",
      "a",
      "b",
    ]);
  });

  it("maps the tree onto percent rectangles with one divider per split", () => {
    const { panes, dividers } = terminalLayoutGeometry(resizeTerminalSplit(twoByOne, [], 0.6));
    expect(panes).toEqual([
      { key: "agent", left: 0, top: 0, width: 60, height: 100 },
      { key: "a", left: 60, top: 0, width: 40, height: 50 },
      { key: "b", left: 60, top: 50, width: 40, height: 50 },
    ]);
    expect(dividers.map((divider) => divider.path)).toEqual([[], [1]]);
    expect(dividers[1].area).toEqual({ left: 60, top: 0, width: 40, height: 100 });
  });

  it("restores only valid, split, duplicate-free layouts", () => {
    expect(parseTerminalSplitState({ layout: twoByOne, focusedKey: "a" })).toEqual({
      layout: twoByOne,
      focusedKey: "a",
    });
    for (const value of [
      { layout: "agent", focusedKey: "agent" },
      { layout: twoByOne, focusedKey: "missing" },
      { layout: { axis: "rows", ratio: 0.9, children: ["a", "b"] }, focusedKey: "a" },
      { layout: { axis: "rows", ratio: 0.5, children: ["a", "a"] }, focusedKey: "a" },
      { layout: { axis: "diagonal", ratio: 0.5, children: ["a", "b"] }, focusedKey: "a" },
      { layout: { axis: "rows", ratio: null, children: ["a", "b"] }, focusedKey: "a" },
      {
        layout: { axis: "rows", ratio: 0.5, children: ["a", { axis: "rows", children: [] }] },
        focusedKey: "a",
      },
      null,
    ]) {
      expect(parseTerminalSplitState(value)).toBeUndefined();
    }
  });
});
