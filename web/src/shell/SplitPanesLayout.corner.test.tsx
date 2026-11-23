// Where a split's vertical and horizontal dividers meet, a corner handle drags both at once,
// stops each way at the minimum on its own, and leaves the dividers' keyboard and ARIA alone.

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type TerminalLayout,
  terminalLayoutGeometry,
  type TerminalSplitState,
} from "@/lib/paneSplitLayout";
import { paneDividerDrag } from "@/shell/paneDividerDrag";
import { SplitPanesLayout } from "@/shell/SplitPanesLayout";

import type { PaneSizing } from "./paneSizing";

// a on the left; b over c on the right, the right half's rows meeting a's divider at 40%, 50%.
const T_SPLIT: TerminalSplitState = {
  layout: {
    axis: "columns",
    ratio: 0.4,
    children: ["a", { axis: "rows", ratio: 0.5, children: ["b", "c"] }],
  },
  focusedKey: "a",
};

function setup({
  split = T_SPLIT,
  zoomedKey = null as string | null,
  cards = false,
  withStrip = false,
  sizing = (() => null) as () => PaneSizing | null,
} = {}) {
  const onResize = vi.fn();
  function View({
    next,
    zoomed = zoomedKey,
  }: {
    next: TerminalSplitState;
    zoomed?: string | null;
  }) {
    const panesRef = useRef<HTMLDivElement | null>(null);
    return (
      <SplitPanesLayout
        conversationId="c1"
        split={next}
        singlePaneKey="a"
        paneIdOf={(key) => key}
        renderPane={(key) => <div data-testid={`content-${key}`} />}
        onResize={onResize}
        panesRef={panesRef}
        zoomedKey={zoomed}
        onUnzoom={() => {}}
        sizing={sizing}
        cards={cards}
        strip={withStrip ? { paneOf: (key) => ({ name: key, commands: null }) } : undefined}
      />
    );
  }
  const rendered = render(<View next={split} />);
  return {
    ...rendered,
    onResize,
    rerender: (next: TerminalSplitState, zoomed: string | null = null) =>
      rendered.rerender(<View next={next} zoomed={zoomed} />),
  };
}

const corners = () => screen.queryAllByTestId("terminal-split-corner");
const pane = (key: string) => document.querySelector<HTMLElement>(`[data-terminal-pane="${key}"]`)!;

/** Presses a corner or divider, or a corner's region, and moves the pointer to (x, y) in a 1000 × 500 px view. */
function dragHandle(corner: HTMLElement, x: number, y: number, pressed: HTMLElement = corner) {
  vi.spyOn(corner.parentElement!, "getBoundingClientRect").mockReturnValue({
    left: 0,
    top: 0,
    width: 1000,
    height: 500,
  } as DOMRect);
  let captured = false;
  corner.setPointerCapture = () => {
    captured = true;
  };
  corner.hasPointerCapture = () => captured;
  fireEvent.pointerDown(pressed, { pointerId: 1 });
  fireEvent.pointerMove(corner, { pointerId: 1, clientX: x, clientY: y });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("a split's divider corner", () => {
  it.each([false, true])("drags both dividers at once, saved as one change (cards %s)", (cards) => {
    const { onResize } = setup({ cards });
    const [corner] = corners();
    expect(corners()).toHaveLength(1);
    expect(corner).toHaveStyle({ left: "calc(40% - 6px)", top: "calc(50% - 6px)" });

    dragHandle(corner, 600, 350);
    // Both ways follow the pointer while it moves; nothing is saved until release.
    expect(pane("b")).toHaveStyle({ left: "60%", width: "40%", height: "70%" });
    expect(pane("a")).toHaveStyle({ width: "60%" });
    expect(onResize).not.toHaveBeenCalled();
    fireEvent.pointerUp(corner, { pointerId: 1 });

    expect(onResize).toHaveBeenCalledTimes(1);
    const [[resizes]] = onResize.mock.calls;
    expect(resizes).toHaveLength(2);
    expect(resizes[0].path).toEqual([]);
    expect(resizes[0].ratio).toBeCloseTo(0.6);
    expect(resizes[1].path).toEqual([1]);
    expect(resizes[1].ratio).toBeCloseTo(0.7);
  });

  it.each([
    ["top-right", "nesw-resize", 300, 150],
    ["bottom-right", "nwse-resize", 300, 350],
  ])(
    "gives the %s pane's corner its own diagonal, and drags both ways from it",
    (quadrant, cursor, x, y) => {
      const { onResize } = setup();
      const [corner] = corners();
      const regions = within(corner).getAllByTestId("terminal-split-corner-region");
      // The stem runs right, so the upper and lower right panes each have a corner here.
      expect(regions.map((region) => region.dataset.quadrant)).toEqual([
        "top-right",
        "bottom-right",
      ]);
      const region = regions.find((candidate) => candidate.dataset.quadrant === quadrant)!;
      expect(region).toHaveStyle({ cursor });

      dragHandle(corner, x, y, region);
      // The pressed diagonal holds for the whole drag, over every region.
      for (const other of regions) expect(other).toHaveStyle({ cursor });
      expect(corner).toHaveStyle({ cursor });
      fireEvent.pointerUp(corner, { pointerId: 1 });

      const [[resizes]] = onResize.mock.calls;
      expect(resizes[0].ratio).toBeCloseTo(x / 1000);
      expect(resizes[1].ratio).toBeCloseTo(y / 500);
      // After release each region shows its own diagonal again.
      expect(regions[0]).toHaveStyle({ cursor: "nesw-resize" });
      expect(regions[1]).toHaveStyle({ cursor: "nwse-resize" });
    },
  );

  it("stops a way at the minimum on its own, and the other way still follows", () => {
    // The left pane can't narrow below a third of the view; heights are free.
    const narrowest = (layout: TerminalLayout) =>
      typeof layout === "string" ? 1 : layout.axis === "columns" ? layout.ratio : 1;
    const sizing = (): PaneSizing => ({
      fits: () => true,
      keeps: (_before, after) => narrowest(after) >= 1 / 3,
      toward: (_target, from) => from,
      refusal: () => "",
    });
    const { onResize } = setup({ sizing });
    const [corner] = corners();
    dragHandle(corner, 100, 350);
    expect(pane("a")).toHaveStyle({ width: "40%" });
    expect(pane("b")).toHaveStyle({ height: "70%" });
    fireEvent.pointerUp(corner, { pointerId: 1 });
    const [[resizes]] = onResize.mock.calls;
    expect(resizes.map(({ path }: { path: number[] }) => path)).toEqual([[], [1]]);
    expect(resizes[0].ratio).toBeCloseTo(0.4);
    expect(resizes[1].ratio).toBeCloseTo(0.7);
  });

  it("keeps dragging an aligned cross after one side stops at its minimum, and saves on release", () => {
    // Left: a1 over a2 over b; right: c over d. Both horizontal dividers cross the vertical at 50%, 50%.
    const split: TerminalSplitState = {
      layout: {
        axis: "columns",
        ratio: 0.5,
        children: [
          {
            axis: "rows",
            ratio: 0.5,
            children: [{ axis: "rows", ratio: 0.5, children: ["a1", "a2"] }, "b"],
          },
          { axis: "rows", ratio: 0.5, children: ["c", "d"] },
        ],
      },
      focusedKey: "a1",
    };
    // Every pane keeps a fifth of the view each way.
    const roomy = (layout: TerminalLayout) =>
      terminalLayoutGeometry(layout).panes.every((area) => area.width >= 20 && area.height >= 20);
    const sizing = (): PaneSizing => ({
      fits: roomy,
      keeps: (_before, after) => roomy(after),
      toward: (_target, from) => from,
      refusal: () => "",
    });
    const { onResize } = setup({ split, sizing });
    const [corner] = corners();
    expect(corner).toHaveStyle({ left: "calc(50% - 6px)", top: "calc(50% - 6px)" });
    // Four panes meet here, each corner with its own diagonal.
    expect(
      within(corner)
        .getAllByTestId("terminal-split-corner-region")
        .map((region) => `${region.dataset.quadrant} ${region.style.cursor}`),
    ).toEqual([
      "top-left nwse-resize",
      "top-right nesw-resize",
      "bottom-left nesw-resize",
      "bottom-right nwse-resize",
    ]);
    // Up to 30%: a1 and a2 would be 15% tall, so the left side stops; the right side follows.
    dragHandle(corner, 600, 150);
    expect(pane("c")).toHaveStyle({ height: "30%" });
    expect(pane("b")).toHaveStyle({ top: "50%" });
    expect(corner.isConnected).toBe(true);
    // The drag goes on with the same handle, though the dividers no longer cross there.
    fireEvent.pointerMove(corner, { pointerId: 1, clientX: 650, clientY: 150 });
    expect(pane("c")).toHaveStyle({ left: "65%" });
    fireEvent.pointerUp(corner, { pointerId: 1 });

    expect(onResize).toHaveBeenCalledTimes(1);
    const [[resizes]] = onResize.mock.calls;
    const byPath = Object.fromEntries(
      resizes.map(({ path, ratio }: { path: number[]; ratio: number }) => [
        path.join(".") || "root",
        ratio,
      ]),
    );
    expect(Object.keys(byPath).sort()).toEqual(["0", "1", "root"]);
    expect(byPath.root).toBeCloseTo(0.65);
    expect(byPath["0"]).toBeCloseTo(0.5);
    expect(byPath["1"]).toBeCloseTo(0.3);
    // Released, the corners are the junctions the new layout has.
    expect(corners().length).toBeGreaterThan(1);
  });

  it("saves where the pointer last was, though the release comes before that move renders", () => {
    const first = setup();
    dragHandle(corners()[0], 700, 400);
    fireEvent.pointerUp(corners()[0], { pointerId: 1 });
    const [[settled]] = first.onResize.mock.calls;
    cleanup();

    const { onResize } = setup();
    const [corner] = corners();
    dragHandle(corner, 600, 350);
    // The last move and the release in one batch: nothing renders between them.
    act(() => {
      fireEvent.pointerMove(corner, { pointerId: 1, clientX: 700, clientY: 400 });
      fireEvent.pointerUp(corner, { pointerId: 1 });
    });
    expect(onResize).toHaveBeenCalledExactlyOnceWith(settled);
  });

  it("discards the drag's sizes when the pointer is cancelled", () => {
    const { onResize } = setup();
    const [corner] = corners();
    dragHandle(corner, 600, 350);
    expect(pane("a")).toHaveStyle({ width: "60%" });
    fireEvent.pointerCancel(corner, { pointerId: 1 });
    expect(pane("a")).toHaveStyle({ width: "40%" });
    expect(pane("b")).toHaveStyle({ height: "50%" });
    expect(paneDividerDrag.dragging()).toBe(false);
    fireEvent.pointerUp(corner, { pointerId: 1 });
    expect(onResize).not.toHaveBeenCalled();
  });

  it("drops a drag that maximizing cut off, so restoring shows the saved sizes", () => {
    const { onResize, rerender } = setup();
    const [corner] = corners();
    dragHandle(corner, 600, 350);
    expect(pane("a")).toHaveStyle({ width: "60%" });
    // Maximizing keeps the same split; the zoom lives apart from it.
    rerender(T_SPLIT, "b");
    expect(corners()).toHaveLength(0);
    expect(paneDividerDrag.dragging()).toBe(false);
    fireEvent.pointerUp(corner, { pointerId: 1 });
    rerender(T_SPLIT, null);
    expect(pane("a")).toHaveStyle({ width: "40%" });
    expect(pane("b")).toHaveStyle({ width: "60%", height: "50%" });
    expect(onResize).not.toHaveBeenCalled();
    // A new drag starts clean.
    const [again] = corners();
    dragHandle(again, 500, 250);
    fireEvent.pointerUp(again, { pointerId: 1 });
    expect(onResize).toHaveBeenCalledTimes(1);
  });

  it("drops a drag whose divider went, so a later split shows its own corners", () => {
    const cross = (right: TerminalLayout): TerminalSplitState => ({
      layout: {
        axis: "columns",
        ratio: 0.5,
        children: [{ axis: "rows", ratio: 0.3, children: ["a", "b"] }, right],
      },
      focusedKey: "a",
    });
    const { onResize, rerender } = setup({
      split: cross({ axis: "rows", ratio: 0.3, children: ["c", "d"] }),
    });
    const [held] = corners();
    dragHandle(held, 500, 150);
    // d closes mid-drag: the right side's divider the drag held is gone.
    rerender(cross("c"));
    expect(paneDividerDrag.dragging()).toBe(false);
    // c splits down at the middle: two Ts, at 30% and at 50%.
    rerender(cross({ axis: "rows", ratio: 0.5, children: ["c", "e"] }));
    expect(corners().map((corner) => corner.style.top)).toEqual([
      "calc(30% - 6px)",
      "calc(50% - 6px)",
    ]);
    expect(onResize).not.toHaveBeenCalled();
  });

  it("leaves a divider past the resize limits where it is as the drag goes further out", () => {
    const pastLimit: TerminalSplitState = {
      layout: { axis: "columns", ratio: 0.9, children: ["a", "b"] },
      focusedKey: "a",
    };
    const { onResize } = setup({ split: pastLimit });
    const [divider] = screen.getAllByTestId("terminal-split-divider");
    dragHandle(divider, 950, 250);
    expect(pane("a")).toHaveStyle({ width: "90%" });
    fireEvent.pointerMove(divider, { pointerId: 1, clientX: 880, clientY: 250 });
    expect(pane("a")).toHaveStyle({ width: "88%" });
    fireEvent.pointerUp(divider, { pointerId: 1 });
    expect(onResize).toHaveBeenCalledWith([{ path: [], ratio: 0.88 }]);
  });

  it("ends a divider's drag once a pane closing puts another split in its place", () => {
    const nested: TerminalSplitState = {
      layout: {
        axis: "columns",
        ratio: 0.3,
        children: ["a", { axis: "columns", ratio: 0.5, children: ["b", "c"] }],
      },
      focusedKey: "b",
    };
    const { onResize, rerender } = setup({ split: nested });
    const [root] = screen.getAllByTestId("terminal-split-divider");
    dragHandle(root, 400, 250);
    expect(pane("a")).toHaveStyle({ width: "40%" });
    // a closes mid-drag: b | c moves up to the root, its divider at the same path the same way.
    const promoted: TerminalSplitState = {
      layout: { axis: "columns", ratio: 0.5, children: ["b", "c"] },
      focusedKey: "b",
    };
    rerender(promoted);
    expect(paneDividerDrag.dragging()).toBe(false);
    fireEvent.pointerMove(root, { pointerId: 1, clientX: 800, clientY: 250 });
    fireEvent.pointerUp(root, { pointerId: 1 });
    expect(pane("b")).toHaveStyle({ width: "50%" });
    expect(onResize).not.toHaveBeenCalled();
  });

  it("is a pointer shortcut only: the dividers keep the keyboard and what assistive tech reads", () => {
    const { onResize } = setup();
    const [corner] = corners();
    expect(corner).toHaveAttribute("aria-hidden", "true");
    expect(corner).not.toHaveAttribute("tabindex");
    const separators = screen.getAllByRole("separator", { name: "Resize terminal panes" });
    expect(separators.map((separator) => separator.getAttribute("aria-orientation"))).toEqual([
      "vertical",
      "horizontal",
    ]);
    for (const separator of separators) expect(separator).toHaveAttribute("tabindex", "0");
    fireEvent.keyDown(separators[0], { key: "ArrowRight" });
    const [[stepped]] = onResize.mock.calls;
    expect(stepped).toEqual([{ path: [], ratio: expect.closeTo(0.43) }]);
  });

  it("shows no corner where the dividers don't meet, or while a pane is maximized", () => {
    setup({
      split: {
        layout: {
          axis: "columns",
          ratio: 0.5,
          children: ["a", { axis: "columns", ratio: 0.5, children: ["b", "c"] }],
        },
        focusedKey: "a",
      },
    });
    expect(corners()).toHaveLength(0);
    cleanup();
    setup({ zoomedKey: "b" });
    expect(corners()).toHaveLength(0);
  });

  it("draws no resize hint between floating panes, as the sidebars' edges don't, but shows focus", () => {
    const line = (divider: HTMLElement) => divider.firstElementChild as HTMLElement;
    setup({ cards: true, withStrip: true });
    const dividers = screen.getAllByTestId("terminal-split-divider");
    expect(dividers).toHaveLength(2);
    for (const divider of dividers) {
      expect(line(divider).className).not.toMatch(/group-hover:/);
      // The keyboard's focus still shows where it is.
      expect(line(divider).className).toMatch(/group-focus-visible:bg-ring/);
    }
    expect(dividers.map((divider) => divider.className)).toEqual([
      expect.stringMatching(/cursor-col-resize/),
      expect.stringMatching(/cursor-row-resize/),
    ]);
    expect(corners()).toHaveLength(1);
    expect(screen.queryByTestId("terminal-split-corner-dot")).toBeNull();
    cleanup();
    // Panes that meet at a drawn line keep its hover, and the corner its dot.
    setup({ withStrip: true });
    for (const divider of screen.getAllByTestId("terminal-split-divider")) {
      expect(line(divider).className).toMatch(/group-hover:bg-ring/);
    }
    expect(screen.getByTestId("terminal-split-corner-dot").className).toMatch(
      /group-hover:bg-ring/,
    );
  });
});
