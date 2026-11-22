import { describe, expect, it } from "vitest";
import {
  closeTerminalPane,
  equalizeTerminalLayout,
  MAX_SPLIT_RATIO,
  moveTerminalPane,
  neighborTerminalPane,
  nudgeTerminalSplit,
  MAX_SAVED_LAYOUT_DEPTH,
  parseTerminalSplitState,
  clampSplitRatio,
  reconcileTerminalSplit,
  removeTerminalLayoutKey,
  resizeTerminalSplit,
  splitTerminalLayout,
  steppedSplitRatio,
  swapTerminalPanes,
  terminalLayoutGeometry,
  terminalLayoutKeys,
  type TerminalLayout,
} from "./paneSplitLayout";

/** Each pane's width (or height, for ``"rows"``) as a share of the view. */
function paneShares(layout: TerminalLayout, axis: "columns" | "rows" = "columns") {
  return Object.fromEntries(
    terminalLayoutGeometry(layout).panes.map((pane) => [
      pane.key,
      (axis === "columns" ? pane.width : pane.height) / 100,
    ]),
  );
}

function expectShares(layout: TerminalLayout, expected: Record<string, number>, axis?: "rows") {
  const shares = paneShares(layout, axis);
  expect(Object.keys(shares).sort()).toEqual(Object.keys(expected).sort());
  for (const [key, share] of Object.entries(expected)) expect(shares[key]).toBeCloseTo(share);
}

const twoByOne: TerminalLayout = {
  axis: "columns",
  ratio: 0.5,
  children: ["agent", { axis: "rows", ratio: 0.5, children: ["a", "b"] }],
};

describe("paneSplitLayout", () => {
  it("splits only the target pane, nesting the new key after it", () => {
    expect(splitTerminalLayout("agent", "agent", "a", "columns")).toEqual({
      axis: "columns",
      ratio: 0.5,
      children: ["agent", "a"],
    });
    const nested = splitTerminalLayout(twoByOne, "b", "c", "columns");
    expect(terminalLayoutKeys(nested)).toEqual(["agent", "a", "b", "c"]);
    expect(splitTerminalLayout(twoByOne, "missing", "c", "rows")).toEqual(twoByOne);
    // Split left or up: the new pane goes first.
    expect(splitTerminalLayout("agent", "agent", "a", "rows", "before")).toEqual({
      axis: "rows",
      ratio: 0.5,
      children: ["a", "agent"],
    });
  });

  it("gives a new pane in a row the size of the pane it splits, scaling the row to fit", () => {
    // 0.8 | 0.2, split the large pane across the row: 4/9 | 4/9 | 1/9.
    const uneven: TerminalLayout = { axis: "columns", ratio: 0.8, children: ["large", "small"] };
    expectShares(splitTerminalLayout(uneven, "large", "new", "columns"), {
      large: 4 / 9,
      new: 4 / 9,
      small: 1 / 9,
    });
    // Splitting the small one, before it, scales the large one the same way: 8/12 | 2/12 | 2/12.
    expectShares(splitTerminalLayout(uneven, "small", "new", "columns", "before"), {
      large: 0.8 / 1.2,
      new: 0.2 / 1.2,
      small: 0.2 / 1.2,
    });
  });

  it("halves only the target when the split runs across its row", () => {
    const uneven: TerminalLayout = { axis: "columns", ratio: 0.8, children: ["large", "small"] };
    const after = splitTerminalLayout(uneven, "large", "new", "rows");
    expectShares(after, { large: 0.8, new: 0.8, small: 0.2 });
    expectShares(after, { large: 0.5, new: 0.5, small: 1 }, "rows");
  });

  it("splits one pane into equal thirds, and back to halves when any one closes", () => {
    const halves = splitTerminalLayout("a", "a", "b", "columns");
    const thirds = splitTerminalLayout(halves, "b", "c", "columns");
    expectShares(thirds, { a: 1 / 3, b: 1 / 3, c: 1 / 3 });
    for (const key of ["a", "b", "c"]) {
      const remaining = removeTerminalLayoutKey(thirds, key)!;
      expectShares(
        remaining,
        Object.fromEntries(["a", "b", "c"].filter((other) => other !== key).map((k) => [k, 0.5])),
      );
    }
  });

  it("reverses a split when the new pane closes, and grows a row past the old quarter floor", () => {
    const uneven: TerminalLayout = { axis: "columns", ratio: 0.8, children: ["large", "small"] };
    const split = splitTerminalLayout(uneven, "large", "new", "columns");
    expectShares(removeTerminalLayoutKey(split, "new")!, { large: 0.8, small: 0.2 });
    // Five equal panes from repeated splits: each takes a fifth, under the old 25% floor.
    let row: TerminalLayout = "p1";
    for (const key of ["p2", "p3", "p4", "p5"])
      row = splitTerminalLayout(row, "p1", key, "columns");
    expectShares(row, { p1: 0.2, p2: 0.2, p3: 0.2, p4: 0.2, p5: 0.2 });
    expectShares(removeTerminalLayoutKey(row, "p3")!, { p1: 0.25, p2: 0.25, p4: 0.25, p5: 0.25 });
  });

  it("keeps proportions inside a nested group that grows", () => {
    // a | (b over c), split a across the row: the stack's rows are untouched.
    const after = splitTerminalLayout(twoByOne, "agent", "new", "columns");
    expectShares(after, { agent: 1 / 3, new: 1 / 3, a: 1 / 3, b: 1 / 3 });
    expectShares(after, { agent: 1, new: 1, a: 0.5, b: 0.5 }, "rows");
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

  it("keeps the remaining panes' proportions in the row a pane leaves", () => {
    // Three equal columns: closing any one leaves two halves.
    const thirds: TerminalLayout = {
      axis: "columns",
      ratio: 1 / 3,
      children: ["a", { axis: "columns", ratio: 0.5, children: ["b", "c"] }],
    };
    for (const key of ["a", "b", "c"]) {
      const after = removeTerminalLayoutKey(thirds, key);
      expect(typeof after !== "string" && after?.ratio).toBeCloseTo(0.5);
    }
    // 0.5 | 0.3 | 0.2: closing the middle keeps 5:2 between the outer two.
    const uneven: TerminalLayout = {
      axis: "columns",
      ratio: 0.5,
      children: ["a", { axis: "columns", ratio: 0.6, children: ["b", "c"] }],
    };
    const after = removeTerminalLayoutKey(uneven, "b");
    expect(typeof after !== "string" && after?.ratio).toBeCloseTo(0.5 / 0.7);
  });

  it("leaves a split the other way unchanged when a pane closes inside it", () => {
    // (a over (b | c)) | d: closing b widens c only within its row.
    const layout: TerminalLayout = {
      axis: "columns",
      ratio: 0.6,
      children: [
        {
          axis: "rows",
          ratio: 0.4,
          children: ["a", { axis: "columns", ratio: 0.5, children: ["b", "c"] }],
        },
        "d",
      ],
    };
    expect(removeTerminalLayoutKey(layout, "b")).toEqual({
      axis: "columns",
      ratio: 0.6,
      children: [{ axis: "rows", ratio: 0.4, children: ["a", "c"] }, "d"],
    });
  });

  it("clamps resized splits to 15..85% of the parent", () => {
    const wide = resizeTerminalSplit(twoByOne, [], 0.95) as Exclude<TerminalLayout, string>;
    expect(wide.ratio).toBe(0.85);
    const tall = resizeTerminalSplit(twoByOne, [1], 0.1) as Exclude<TerminalLayout, string>;
    expect((tall.children[1] as Exclude<TerminalLayout, string>).ratio).toBe(0.15);
    expect(clampSplitRatio(Number.NaN)).toBe(0.5);
  });

  it("keeps a split past a limit where it is when dragged further out, and lets it move back", () => {
    const pastLimit: TerminalLayout = { axis: "columns", ratio: 0.9, children: ["a", "b"] };
    const ratioOf = (layout: TerminalLayout) => (layout as Exclude<TerminalLayout, string>).ratio;
    expect(ratioOf(resizeTerminalSplit(pastLimit, [], 0.95))).toBe(0.9);
    expect(ratioOf(resizeTerminalSplit(pastLimit, [], 0.88))).toBe(0.88);
    expect(ratioOf(resizeTerminalSplit(pastLimit, [], 0.5))).toBe(0.5);
    const lowRow: TerminalLayout = { ...pastLimit, ratio: 0.1 };
    expect(ratioOf(resizeTerminalSplit(lowRow, [], 0.05))).toBe(0.1);
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
    // An exited shell closes like a closed pane: focus goes to the pane taking its space.
    expect(reconcileTerminalSplit(state, live, options(true))).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: ["agent", "a"] },
      focusedKey: "a",
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

  it("restores only valid, split, duplicate-free layouts", () => {
    expect(parseTerminalSplitState({ layout: twoByOne, focusedKey: "a" })).toEqual({
      layout: twoByOne,
      focusedKey: "a",
    });
    // A long row's splits sit past the resize limits, and still restore.
    const longRow = { axis: "columns", ratio: 0.1, children: ["a", "b"] };
    expect(parseTerminalSplitState({ layout: longRow, focusedKey: "a" })?.layout).toEqual(longRow);
    for (const value of [
      { layout: "agent", focusedKey: "agent" },
      { layout: twoByOne, focusedKey: "missing" },
      { layout: { axis: "rows", ratio: 0, children: ["a", "b"] }, focusedKey: "a" },
      { layout: { axis: "rows", ratio: 1, children: ["a", "b"] }, focusedKey: "a" },
      { layout: { axis: "rows", ratio: Number.NaN, children: ["a", "b"] }, focusedKey: "a" },
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

  it("restores a split nested past the depth limit as unsplit, without overflowing", () => {
    const nested = (depth: number): unknown =>
      Array.from({ length: depth }).reduce<unknown>(
        (inner, _, index) => ({ axis: "rows", ratio: 0.5, children: [`p${index}`, inner] }),
        "last",
      );
    expect(
      parseTerminalSplitState({ layout: nested(MAX_SAVED_LAYOUT_DEPTH), focusedKey: "last" }),
    ).toBeDefined();
    expect(
      parseTerminalSplitState({ layout: nested(MAX_SAVED_LAYOUT_DEPTH + 1), focusedKey: "last" }),
    ).toBeUndefined();
    expect(
      parseTerminalSplitState({ layout: nested(100_000), focusedKey: "last" }),
    ).toBeUndefined();
  });
});

describe("neighborTerminalPane", () => {
  // agent | (a / b): agent on the left, a above b on the right.
  it("moves to the pane beside, without wrapping at the edges", () => {
    expect(neighborTerminalPane(twoByOne, "agent", "right")).toBe("a");
    expect(neighborTerminalPane(twoByOne, "b", "left")).toBe("agent");
    expect(neighborTerminalPane(twoByOne, "a", "down")).toBe("b");
    expect(neighborTerminalPane(twoByOne, "b", "up")).toBe("a");
    expect(neighborTerminalPane(twoByOne, "agent", "left")).toBeNull();
    expect(neighborTerminalPane(twoByOne, "a", "right")).toBeNull();
    expect(neighborTerminalPane(twoByOne, "a", "up")).toBeNull();
    expect(neighborTerminalPane(twoByOne, "missing", "right")).toBeNull();
  });

  it("prefers the pane overlapping it most, then the nearer center, then layout order", () => {
    // a above b on the left, c on the right lower two thirds: from c, b overlaps most.
    const layout: TerminalLayout = {
      axis: "columns",
      ratio: 0.5,
      children: [
        { axis: "rows", ratio: 0.5, children: ["a", "b"] },
        { axis: "rows", ratio: 0.3, children: ["top", "c"] },
      ],
    };
    expect(neighborTerminalPane(layout, "c", "left")).toBe("b");
    // Equal overlaps tie to the first in layout order.
    expect(neighborTerminalPane(twoByOne, "agent", "right")).toBe("a");
  });

  it("treats edges flush within rounding as touching, so the overlap still decides", () => {
    // p | q over s on the left, f on the right: q's right edge lands at 0.6 only to rounding.
    const layout: TerminalLayout = {
      axis: "columns",
      ratio: 0.6,
      children: [
        {
          axis: "rows",
          ratio: 0.7,
          children: [{ axis: "columns", ratio: 5 / 12, children: ["p", "q"] }, "s"],
        },
        "f",
      ],
    };
    expect(neighborTerminalPane(layout, "f", "left")).toBe("q");
  });

  it("measures distance in the view's real proportions", () => {
    // a | b over a wide c: from c upward, a and b overlap equally; layout order decides.
    const layout: TerminalLayout = {
      axis: "rows",
      ratio: 0.5,
      children: [{ axis: "columns", ratio: 0.5, children: ["a", "b"] }, "c"],
    };
    expect(neighborTerminalPane(layout, "c", "up", 3)).toBe("a");
    // A wide view scales every candidate alike, so the pane directly above still wins.
    const diagonal: TerminalLayout = {
      axis: "columns",
      ratio: 0.5,
      children: [
        { axis: "rows", ratio: 0.75, children: ["near", "from"] },
        { axis: "rows", ratio: 0.25, children: ["far", "below"] },
      ],
    };
    expect(neighborTerminalPane(diagonal, "from", "up")).toBe("near");
  });
});

describe("nudgeTerminalSplit", () => {
  it("moves the nearest divider on the direction's axis, clamped at the limits", () => {
    const moved = nudgeTerminalSplit(twoByOne, "a", "down");
    expect(moved).toEqual({
      ...twoByOne,
      children: ["agent", { axis: "rows", ratio: 0.53, children: ["a", "b"] }],
    });
    // Left/right skips the rows split and moves the root divider, whichever side a is on.
    const left = nudgeTerminalSplit(twoByOne, "a", "left");
    expect(typeof left !== "string" && left.ratio).toBeCloseTo(0.47);
    const atLimit: TerminalLayout = {
      axis: "columns",
      ratio: MAX_SPLIT_RATIO,
      children: ["x", "y"],
    };
    expect(nudgeTerminalSplit(atLimit, "x", "right")).toBe(atLimit);
  });

  it("keeps the other panes' ratios in the group it resizes", () => {
    // a | b | c in thirds: growing a shrinks b and c together, keeping them equal.
    const thirds = equalizeTerminalLayout({
      axis: "columns",
      ratio: 0.5,
      children: ["a", { axis: "columns", ratio: 0.5, children: ["b", "c"] }],
    });
    const grown = nudgeTerminalSplit(thirds, "a", "right");
    const shares = paneShares(grown);
    expect(shares.a).toBeCloseTo(1 / 3 + 0.03);
    expect(shares.b).toBeCloseTo(shares.c);
    // The divider nearest b is the one between b and c, which leaves a alone.
    expect(paneShares(nudgeTerminalSplit(thirds, "b", "right")).a).toBeCloseTo(1 / 3);
  });

  it("never steps a split past a limit the other way", () => {
    // A 10% split, as in a row of seven or more: shrinking stops, growing moves it one step.
    const narrow: TerminalLayout = { axis: "columns", ratio: 0.1, children: ["x", "y"] };
    expect(nudgeTerminalSplit(narrow, "x", "left")).toBe(narrow);
    expect(steppedSplitRatio(0.1, -0.03)).toBeNull();
    expect(steppedSplitRatio(0.1, 0.03)).toBeCloseTo(0.15);
    expect(steppedSplitRatio(0.5, 0.03)).toBeCloseTo(0.53);
    expect(steppedSplitRatio(0.85, 0.03)).toBeNull();
  });

  it("does nothing without a divider on that axis", () => {
    const columns: TerminalLayout = { axis: "columns", ratio: 0.5, children: ["x", "y"] };
    expect(nudgeTerminalSplit(columns, "x", "up")).toBe(columns);
    expect(nudgeTerminalSplit("agent", "agent", "right")).toBe("agent");
    expect(nudgeTerminalSplit(columns, "missing", "right")).toBe(columns);
  });
});

describe("equalizeTerminalLayout", () => {
  it("gives every pane the same share along each axis", () => {
    const chain: TerminalLayout = {
      axis: "columns",
      ratio: 0.5,
      children: ["a", { axis: "columns", ratio: 0.7, children: ["b", "c"] }],
    };
    const equal = equalizeTerminalLayout(chain);
    expect(typeof equal !== "string" && equal.ratio).toBeCloseTo(1 / 3);
    expect(
      typeof equal !== "string" && typeof equal.children[1] !== "string" && equal.children[1].ratio,
    ).toBe(0.5);
    // A stack beside one pane splits the width in half; the stack shares its height.
    expect(equalizeTerminalLayout({ ...twoByOne, ratio: 0.7 })).toEqual(twoByOne);
  });

  it("counts a group split the other way as one cell", () => {
    // a | ((b | c) over d): the stack is one cell beside a, so the width halves.
    const layout: TerminalLayout = {
      axis: "columns",
      ratio: 0.3,
      children: [
        "a",
        {
          axis: "rows",
          ratio: 0.7,
          children: [{ axis: "columns", ratio: 0.2, children: ["b", "c"] }, "d"],
        },
      ],
    };
    expect(equalizeTerminalLayout(layout)).toEqual({
      axis: "columns",
      ratio: 0.5,
      children: [
        "a",
        {
          axis: "rows",
          ratio: 0.5,
          children: [{ axis: "columns", ratio: 0.5, children: ["b", "c"] }, "d"],
        },
      ],
    });
  });

  it("gives each of seven panes in a row a seventh, past the 15% resize limit", () => {
    const deep: TerminalLayout = {
      axis: "columns",
      ratio: 0.5,
      children: [
        "a",
        {
          axis: "columns",
          ratio: 0.5,
          children: ["b", { axis: "columns", ratio: 0.5, children: ["c", "d"] }],
        },
      ],
    };
    let seven: TerminalLayout = deep;
    for (const key of ["e", "f", "g"]) seven = splitTerminalLayout(seven, "d", key, "columns");
    const equal = equalizeTerminalLayout(seven);
    const keys = ["a", "b", "c", "d", "e", "f", "g"];
    expectShares(equal, Object.fromEntries(keys.map((key) => [key, 1 / 7])));
    // The first pane's split, one against six, is itself past the limit.
    expect(typeof equal !== "string" && equal.ratio).toBeCloseTo(1 / 7);
  });
});

describe("closeTerminalPane", () => {
  it("focuses the leaf of the sibling subtree next to the closed pane", () => {
    expect(closeTerminalPane({ layout: twoByOne, focusedKey: "agent" }, "agent")).toEqual({
      layout: { axis: "rows", ratio: 0.5, children: ["a", "b"] },
      focusedKey: "a",
    });
    expect(closeTerminalPane({ layout: twoByOne, focusedKey: "b" }, "b")).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: ["agent", "a"] },
      focusedKey: "a",
    });
    const wide: TerminalLayout = {
      axis: "columns",
      ratio: 0.5,
      children: [{ axis: "rows", ratio: 0.5, children: ["x", "y"] }, "z"],
    };
    // The second child's sibling hands over its last leaf, the one beside it.
    expect(closeTerminalPane({ layout: wide, focusedKey: "z" }, "z")?.focusedKey).toBe("y");
  });

  it("keeps focus elsewhere, collapses to unsplit at one pane, and ignores unknown keys", () => {
    expect(closeTerminalPane({ layout: twoByOne, focusedKey: "agent" }, "a")?.focusedKey).toBe(
      "agent",
    );
    const two = {
      layout: { axis: "rows", ratio: 0.5, children: ["agent", "a"] },
      focusedKey: "a",
    } as const;
    expect(
      closeTerminalPane({ ...two, layout: { ...two.layout, children: ["agent", "a"] } }, "a"),
    ).toBeUndefined();
    const state = { layout: twoByOne, focusedKey: "a" };
    expect(closeTerminalPane(state, "missing")).toBe(state);
  });
});

describe("swapTerminalPanes", () => {
  const NESTED: TerminalLayout = {
    axis: "columns",
    ratio: 0.3,
    children: ["a", { axis: "rows", ratio: 0.7, children: ["b", "c"] }],
  };

  it("trades the two panes' places and keeps every divider where it was", () => {
    expect(swapTerminalPanes(NESTED, "a", "c")).toEqual({
      axis: "columns",
      ratio: 0.3,
      children: ["c", { axis: "rows", ratio: 0.7, children: ["b", "a"] }],
    });
    expect(swapTerminalPanes(NESTED, "c", "b")).toEqual({
      axis: "columns",
      ratio: 0.3,
      children: ["a", { axis: "rows", ratio: 0.7, children: ["c", "b"] }],
    });
  });

  it("leaves the layout itself for the same pane, a missing pane or a lone pane", () => {
    expect(swapTerminalPanes(NESTED, "a", "a")).toBe(NESTED);
    expect(swapTerminalPanes(NESTED, "a", "gone")).toBe(NESTED);
    expect(swapTerminalPanes(NESTED, "gone", "b")).toBe(NESTED);
    expect(swapTerminalPanes("a", "a", "b")).toBe("a");
  });
});

describe("moveTerminalPane", () => {
  const AB: TerminalLayout = { axis: "rows", ratio: 0.5, children: ["a", "b"] };
  const keys = (layout: TerminalLayout) => [...terminalLayoutKeys(layout)].sort();

  it("puts the pane on the side of the target it was dropped on, in half the target", () => {
    expect(moveTerminalPane(AB, "b", "a", "right")).toEqual({
      axis: "columns",
      ratio: 0.5,
      children: ["a", "b"],
    });
    expect(moveTerminalPane(AB, "b", "a", "left")).toEqual({
      axis: "columns",
      ratio: 0.5,
      children: ["b", "a"],
    });
    const sideBySide: TerminalLayout = { axis: "columns", ratio: 0.5, children: ["a", "b"] };
    expect(moveTerminalPane(sideBySide, "a", "b", "bottom")).toEqual({
      axis: "rows",
      ratio: 0.5,
      children: ["b", "a"],
    });
    expect(moveTerminalPane(sideBySide, "a", "b", "top")).toEqual({
      axis: "rows",
      ratio: 0.5,
      children: ["a", "b"],
    });
  });

  it("moves a pane across a nested row and keeps every other pane", () => {
    // a | (b / c): c goes left of a.
    const layout: TerminalLayout = {
      axis: "columns",
      ratio: 0.5,
      children: ["a", { axis: "rows", ratio: 0.5, children: ["b", "c"] }],
    };
    const moved = moveTerminalPane(layout, "c", "a", "left");
    expect(moved).toEqual({
      axis: "columns",
      ratio: 0.5,
      children: [{ axis: "columns", ratio: 0.5, children: ["c", "a"] }, "b"],
    });
    expect(keys(moved)).toEqual(["a", "b", "c"]);
  });

  it("moves a pane beside its own sibling, and onto an ancestor's other branch", () => {
    // (a / b) | c: b goes right of a, inside the same branch.
    const layout: TerminalLayout = {
      axis: "columns",
      ratio: 0.6,
      children: [{ axis: "rows", ratio: 0.5, children: ["a", "b"] }, "c"],
    };
    expect(moveTerminalPane(layout, "b", "a", "right")).toEqual({
      axis: "columns",
      ratio: 0.6,
      children: [{ axis: "columns", ratio: 0.5, children: ["a", "b"] }, "c"],
    });
    // a goes below c, out of its branch: the branch collapses to b.
    expect(moveTerminalPane(layout, "a", "c", "bottom")).toEqual({
      axis: "columns",
      ratio: 0.6,
      children: ["b", { axis: "rows", ratio: 0.5, children: ["c", "a"] }],
    });
  });

  it("returns the layout itself for a drop that changes nothing", () => {
    expect(moveTerminalPane(AB, "a", "a", "left")).toBe(AB);
    expect(moveTerminalPane(AB, "x", "a", "left")).toBe(AB);
    expect(moveTerminalPane(AB, "a", "x", "left")).toBe(AB);
    expect(moveTerminalPane("a", "a", "a", "left")).toBe("a");
    // b is already below a: its ratio stays too.
    const tall: TerminalLayout = { axis: "rows", ratio: 0.7, children: ["a", "b"] };
    expect(moveTerminalPane(tall, "b", "a", "bottom")).toBe(tall);
  });
});
