import { describe, expect, it } from "vitest";

import { type TerminalLayout, terminalLayoutGeometry } from "@/lib/paneSplitLayout";

import {
  applyDividerResizes,
  cornerDragResizes,
  dividerCorners,
  dividerDragSession,
  followCorner,
  holdsItsDividers,
} from "./paneDividerCorners";

const columns = (first: TerminalLayout, second: TerminalLayout, ratio = 0.5): TerminalLayout => ({
  axis: "columns",
  ratio,
  children: [first, second],
});
const rows = (first: TerminalLayout, second: TerminalLayout, ratio = 0.5): TerminalLayout => ({
  axis: "rows",
  ratio,
  children: [first, second],
});
const cornersOf = (layout: TerminalLayout) =>
  dividerCorners(terminalLayoutGeometry(layout).dividers).map(
    ({ left, top, regions, dividers }) => ({
      left,
      top,
      cursors: regions.map(({ quadrant, cursor }) => `${quadrant} ${cursor}`),
      paths: dividers.map((divider) => divider.path),
    }),
  );
const always = () => true;
const TL = "top-left nwse-resize";
const TR = "top-right nesw-resize";
const BL = "bottom-left nesw-resize";
const BR = "bottom-right nwse-resize";

describe("dividerCorners", () => {
  it("finds no corner where dividers run one way", () => {
    expect(cornersOf("a")).toEqual([]);
    expect(cornersOf(columns("a", columns("b", "c")))).toEqual([]);
    expect(cornersOf(rows("a", rows("b", "c")))).toEqual([]);
  });

  it("finds where a nested split's divider ends on its parent's, with both panes' diagonals", () => {
    // Stem to the right: the upper right pane's bottom-left, the lower one's top-left.
    expect(cornersOf(columns("a", rows("b", "c"), 0.4))).toEqual([
      { left: 40, top: 50, cursors: [TR, BR], paths: [[], [1]] },
    ]);
    // Stem to the left: the upper left pane's bottom-right, the lower one's top-right.
    expect(cornersOf(columns(rows("a", "b", 0.3), "c", 0.6))).toEqual([
      { left: 60, top: 30, cursors: [TL, BL], paths: [[], [0]] },
    ]);
    // Stem down: the lower left pane's top-right, the lower right one's top-left.
    expect(cornersOf(rows("a", columns("b", "c", 0.25)))).toEqual([
      { left: 25, top: 50, cursors: [BL, BR], paths: [[], [1]] },
    ]);
    // Stem up: the upper left pane's bottom-right, the upper right one's bottom-left.
    expect(cornersOf(rows(columns("a", "b", 0.7), "c"))).toEqual([
      { left: 70, top: 50, cursors: [TL, TR], paths: [[], [0]] },
    ]);
  });

  it("makes aligned dividers crossing one corner that moves them all", () => {
    expect(cornersOf(columns(rows("a", "b"), rows("c", "d")))).toEqual([
      { left: 50, top: 50, cursors: [TL, TR, BL, BR], paths: [[], [0], [1]] },
    ]);
    // Out of line, each nested divider meets the parent at its own point.
    expect(cornersOf(columns(rows("a", "b", 0.3), rows("c", "d", 0.6))).map((c) => c.top)).toEqual([
      30, 60,
    ]);
  });

  it("finds corners deep in the tree, and none between dividers that only pass each other", () => {
    // Left half split in rows; its bottom split in columns: a T inside a T.
    const layout = columns(rows("a", columns("b", "c")), "d");
    expect(cornersOf(layout)).toEqual([
      { left: 50, top: 50, cursors: [TL, BL], paths: [[], [0]] },
      { left: 25, top: 50, cursors: [BL, BR], paths: [[0], [0, 1]] },
    ]);
  });

  it("finds no corner where a divider meets the window's edge", () => {
    // Each divider ends on the view's border, not on another divider.
    expect(cornersOf(columns("a", "b", 0.3))).toEqual([]);
    expect(cornersOf(rows("a", "b", 0.7))).toEqual([]);
  });
});

describe("a corner's regions", () => {
  const regionsOf = (layout: TerminalLayout) =>
    dividerCorners(terminalLayoutGeometry(layout).dividers).map((corner) =>
      corner.regions.map(({ quadrant, left, top, width, height }) => ({
        quadrant,
        box: [left, top, width, height],
      })),
    );

  it("splits a T's handle in halves along its stem, one per pane corner", () => {
    // Stem down, as two panes side by side under one: left and right halves.
    expect(regionsOf(rows("a", columns("b", "c")))).toEqual([
      [
        { quadrant: "bottom-left", box: [0, 0, 50, 100] },
        { quadrant: "bottom-right", box: [50, 0, 50, 100] },
      ],
    ]);
    // Stem to the right: top and bottom halves.
    expect(regionsOf(columns("a", rows("b", "c")))).toEqual([
      [
        { quadrant: "top-right", box: [0, 0, 100, 50] },
        { quadrant: "bottom-right", box: [0, 50, 100, 50] },
      ],
    ]);
  });

  it("splits a cross's handle in quadrants, one per pane", () => {
    expect(regionsOf(columns(rows("a", "b"), rows("c", "d")))).toEqual([
      [
        { quadrant: "top-left", box: [0, 0, 50, 50] },
        { quadrant: "top-right", box: [50, 0, 50, 50] },
        { quadrant: "bottom-left", box: [0, 50, 50, 50] },
        { quadrant: "bottom-right", box: [50, 50, 50, 50] },
      ],
    ]);
  });
});

describe("followCorner", () => {
  const dividersOf = (layout: TerminalLayout) => terminalLayoutGeometry(layout).dividers;

  it("keeps a held cross whole where its dividers parted, at the dividers it took", () => {
    const [cross] = dividerCorners(dividersOf(columns(rows("a", "b"), rows("c", "d"))));
    const parted = dividersOf(columns(rows("a", "b", 0.5), rows("c", "d", 0.3), 0.6));
    expect(dividerCorners(parted).map((corner) => corner.key)).not.toContain(cross.key);
    const held = followCorner(cross, parted)!;
    expect(held.key).toBe(cross.key);
    expect(held.dividers.map((divider) => divider.path)).toEqual([[], [0], [1]]);
    // Placed by the vertical and the first horizontal divider it holds.
    expect([held.left, held.top]).toEqual([60, 50]);
  });

  it("keeps a held T as it was where it came into line with another", () => {
    const [first] = dividerCorners(dividersOf(columns(rows("a", "b", 0.3), rows("c", "d", 0.6))));
    const aligned = dividersOf(columns(rows("a", "b", 0.6), rows("c", "d", 0.6)));
    const held = followCorner(first, aligned)!;
    expect(held.key).toBe(first.key);
    expect(held.dividers.map((divider) => divider.path)).toEqual([[], [0]]);
    expect(held.top).toBeCloseTo(60);
  });

  it("holds nothing once one of its dividers is gone", () => {
    const [corner] = dividerCorners(dividersOf(columns("a", rows("b", "c"))));
    expect(followCorner(corner, dividersOf(columns("a", "b")))).toBeNull();
    expect(followCorner(corner, dividersOf(columns("a", columns("b", "c"))))).toBeNull();
  });
});

describe("cornerDragResizes", () => {
  it("moves both dividers to the pointer, the nested one within its parent's new area", () => {
    const layout = columns("a", rows("b", "c"), 0.4);
    const [corner] = dividerCorners(terminalLayoutGeometry(layout).dividers);
    expect(cornerDragResizes(layout, corner, { left: 60, top: 70 }, always)).toEqual([
      { path: [], ratio: 0.6 },
      { path: [1], ratio: 0.7 },
    ]);
  });

  it.each([
    ["up and left", { left: 15, top: 30 }],
    ["up and right", { left: 40, top: 30 }],
    ["down and left", { left: 15, top: 70 }],
    ["down and right", { left: 40, top: 70 }],
  ])("drags a T %s, moving both dividers", (_way, point) => {
    // Two panes side by side under one, their divider at 25%, the rows at 50%.
    const layout = rows("a", columns("b", "c", 0.25));
    const [corner] = dividerCorners(terminalLayoutGeometry(layout).dividers);
    const moved = applyDividerResizes(layout, cornerDragResizes(layout, corner, point, always));
    const [held] = dividerCorners(terminalLayoutGeometry(moved).dividers);
    expect(held.left).toBeCloseTo(point.left);
    expect(held.top).toBeCloseTo(point.top);
  });

  it("keeps a nested column divider under the pointer as the outer rows split moves", () => {
    // The vertical divider is nested in the lower half, so its area moves with the rows split.
    const layout = rows("a", columns("b", "c", 0.25));
    const [corner] = dividerCorners(terminalLayoutGeometry(layout).dividers);
    const resizes = cornerDragResizes(layout, corner, { left: 40, top: 60 }, always);
    expect(resizes).toEqual([
      { path: [], ratio: 0.6 },
      { path: [1], ratio: 0.4 },
    ]);
  });

  it("moves the other way alone where one way would leave a pane too small", () => {
    const layout = columns("a", rows("b", "c"), 0.4);
    const [corner] = dividerCorners(terminalLayoutGeometry(layout).dividers);
    // Only the vertical move is refused, as a column too narrow.
    const keeps = (_before: TerminalLayout, after: TerminalLayout) =>
      typeof after === "string" || after.ratio >= 0.3;
    expect(cornerDragResizes(layout, corner, { left: 10, top: 70 }, keeps)).toEqual([
      { path: [], ratio: 0.4 },
      { path: [1], ratio: 0.7 },
    ]);
    expect(cornerDragResizes(layout, corner, { left: 10, top: 70 }, () => false)).toEqual([
      { path: [], ratio: 0.4 },
      { path: [1], ratio: 0.5 },
    ]);
  });

  it("keeps a proportional split's ratio past the resize limits when its move is refused", () => {
    // Splitting along a row keeps proportions, so nine columns beside a stack sit at 0.9.
    const layout = columns("a", rows("b", "c"), 0.9);
    const [corner] = dividerCorners(terminalLayoutGeometry(layout).dividers);
    const resizes = cornerDragResizes(layout, corner, { left: 50, top: 70 }, () => false);
    expect(resizes).toEqual([
      { path: [], ratio: 0.9 },
      { path: [1], ratio: 0.5 },
    ]);
    expect(applyDividerResizes(layout, resizes)).toBe(layout);
  });

  it("holds a move past the limits at the limit, and a split already past it where it is", () => {
    expect(applyDividerResizes(columns("a", rows("b", "c")), [{ path: [], ratio: 0.95 }])).toEqual(
      columns("a", rows("b", "c"), 0.85),
    );
    const pastLimit = columns("a", rows("b", "c"), 0.9);
    expect(applyDividerResizes(pastLimit, [{ path: [], ratio: 0.95 }])).toEqual(pastLimit);
  });

  it("applies resizes in turn", () => {
    const layout = columns("a", rows("b", "c"), 0.4);
    expect(
      applyDividerResizes(layout, [
        { path: [], ratio: 0.6 },
        { path: [1], ratio: 0.7 },
      ]),
    ).toEqual(columns("a", rows("b", "c", 0.7), 0.6));
  });
});

describe("divider drag sessions", () => {
  const dividersOf = (layout: TerminalLayout) => terminalLayoutGeometry(layout).dividers;

  it("holds a corner's dividers, or one divider, with no sizes before the pointer moves", () => {
    const dividers = dividersOf(columns("a", rows("b", "c")));
    const [corner] = dividerCorners(dividers);
    const layout = columns("a", rows("b", "c"));
    expect(dividerDragSession(corner, layout)).toEqual({
      held: [
        { path: [], axis: "columns", sides: [["a"], ["b", "c"]] },
        { path: [1], axis: "rows", sides: [["b"], ["c"]] },
      ],
      corner,
      resizes: null,
    });
    expect(dividerDragSession(dividers[1], layout)).toEqual({
      held: [{ path: [1], axis: "rows", sides: [["b"], ["c"]] }],
      corner: null,
      resizes: null,
    });
  });

  it("lives while every divider it holds is there the same way, wherever it moved", () => {
    const layout = columns("a", rows("b", "c"));
    const [corner] = dividerCorners(dividersOf(layout));
    const session = dividerDragSession(corner, layout);
    expect(holdsItsDividers(session, columns("a", rows("b", "c", 0.8), 0.2))).toBe(true);
    expect(holdsItsDividers(session, columns("a", "b"))).toBe(false);
    expect(holdsItsDividers(session, columns("a", columns("b", "c")))).toBe(false);
  });

  it("ends when another split takes the held one's place, though the path and axis match", () => {
    const layout = columns("a", columns("b", "c"));
    const [root] = dividersOf(layout);
    const session = dividerDragSession(root, layout);
    // a closes: b | c is promoted to the root, its divider at the same path the same way.
    expect(holdsItsDividers(session, columns("b", "c"))).toBe(false);
    // c closes: the root still parts a from what is left beside it.
    expect(holdsItsDividers(session, columns("a", "b"))).toBe(true);
  });
});
