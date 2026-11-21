import { describe, expect, it, vi } from "vitest";

import {
  moveTerminalPane,
  type TerminalLayout,
  type TerminalSplitState,
} from "@/lib/paneSplitLayout";

import {
  MOVE_PANE_LABEL,
  movePaneAvailability,
  movePaneCommand,
  moveSplitPane,
  NO_ROOM_TO_MOVE_REASON,
  placedPaneArea,
  paneDropSpot,
  paneMoveDestinations,
} from "./paneMove";
import type { PaneSizing } from "./paneSizing";
import { toggleZoomedPane, zoomedPaneKey } from "./terminalPaneZoom";
import type { SplitTarget } from "./terminalSplitTarget";

const AB: TerminalLayout = { axis: "columns", ratio: 0.5, children: ["a", "b"] };
const ABC: TerminalLayout = {
  axis: "columns",
  ratio: 0.5,
  children: ["a", { axis: "rows", ratio: 0.5, children: ["b", "c"] }],
};

/** A rule that keeps only layouts `allowed` says yes to. */
const sizingThat = (allowed: (after: TerminalLayout) => boolean): PaneSizing => ({
  fits: allowed,
  keeps: (_before, after) => allowed(after),
  toward: (_target, from) => from,
  refusal: () => "no room",
});

const NO_ROOM = sizingThat(() => false);

const spots = (layout: TerminalLayout, key: string, sizing: PaneSizing | null = null) =>
  paneMoveDestinations(layout, key, sizing).map(({ targetKey, side }) => `${targetKey}:${side}`);

describe("paneMoveDestinations", () => {
  it("lists every side of every other pane that moves the pane, in layout order", () => {
    // b is already right of a: that spot changes nothing, so it isn't offered.
    expect(spots(AB, "b")).toEqual(["a:left", "a:top", "a:bottom"]);
    expect(spots(ABC, "c")).toEqual([
      "a:left",
      "a:right",
      "a:top",
      "a:bottom",
      "b:left",
      "b:right",
      "b:top",
    ]);
  });

  it("leaves out spots the size rule refuses", () => {
    // Only c left or right of a keeps room.
    const roomy = [
      moveTerminalPane(ABC, "c", "a", "left"),
      moveTerminalPane(ABC, "c", "a", "right"),
    ];
    const sizing = sizingThat((after) =>
      roomy.some((layout) => JSON.stringify(layout) === JSON.stringify(after)),
    );
    expect(spots(ABC, "c", sizing)).toEqual(["a:left", "a:right"]);
    expect(spots(ABC, "c", NO_ROOM)).toEqual([]);
  });

  it("offers nothing for a pane that isn't in the split", () => {
    expect(spots(AB, "x")).toEqual([]);
  });
});

describe("movePaneCommand", () => {
  const source = (
    layout: { current: TerminalLayout },
    move = vi.fn(),
    zoomed: { current: string | null } = { current: null },
  ) => ({
    read: () => ({ layout: layout.current, zoomedKey: zoomed.current, key: "c" }),
    sizing: () => null,
    nameOf: (key: string) => `shell ${key}`,
    move: (destination: { key: string; targetKey: string; side: string }) => move(destination),
  });

  it("offers each destination by the target's name, and moves there", () => {
    const move = vi.fn();
    const command = movePaneCommand("move", source({ current: ABC }, move), { available: true });
    expect(command.label).toBe(MOVE_PANE_LABEL);
    const options = command.options!();
    expect(options.map((option) => option.label)).toEqual([
      "Left of shell a",
      "Right of shell a",
      "Above shell a",
      "Below shell a",
      "Left of shell b",
      "Right of shell b",
      "Above shell b",
    ]);
    // Listing the spots moves nothing; only the chosen one runs.
    expect(move).not.toHaveBeenCalled();
    options.find((option) => option.label === "Above shell b")!.run();
    expect(move).toHaveBeenCalledExactlyOnceWith({ key: "c", targetKey: "b", side: "top" });
  });

  it("reads the split again when asked, so a pane closed meanwhile isn't offered", () => {
    const layout = { current: ABC };
    const command = movePaneCommand("move", source(layout), { available: true });
    const before = command.options!().map((option) => option.id);
    layout.current = { axis: "columns", ratio: 0.5, children: ["a", "c"] };
    const after = command.options!().map((option) => option.id);
    expect(before.some((id) => id.startsWith("c>b:"))).toBe(true);
    expect(after.some((id) => id.startsWith("c>b:"))).toBe(false);
  });

  it("offers nothing once a pane is maximized, though the options page was open", () => {
    const zoomed = { current: null as string | null };
    const command = movePaneCommand("move", source({ current: ABC }, vi.fn(), zoomed), {
      available: true,
    });
    expect(command.options!()).not.toHaveLength(0);
    zoomed.current = "c";
    expect(command.options!()).toEqual([]);
  });

  it("is listed for a split, greyed without room, and absent for a lone or maximized pane", () => {
    const availability = (
      layout: TerminalLayout | null,
      key: string,
      sizing: PaneSizing | null = null,
      zoomedKey: string | null = null,
    ) => movePaneAvailability({ layout, zoomedKey, key, sizing });
    expect(availability(ABC, "c")).toEqual({ available: true });
    expect(availability(ABC, "c", NO_ROOM)).toEqual({
      available: false,
      reason: NO_ROOM_TO_MOVE_REASON,
    });
    expect(availability("a", "a")).toBeNull();
    expect(availability(ABC, "x")).toBeNull();
    expect(availability(null, "a")).toBeNull();
    expect(availability(ABC, "c", null, "a")).toBeNull();
    expect(availability(ABC, "c", NO_ROOM, "c")).toBeNull();
  });
});

describe("moveSplitPane", () => {
  const targetWith = (state: TerminalSplitState) => {
    let current: TerminalSplitState | undefined = state;
    const target: SplitTarget = {
      id: `move-${Math.random()}`,
      read: () => current,
      update: (update) => {
        current = update(current);
      },
    };
    return target;
  };

  it("moves the pane to a spot still offered, keeping it the focused pane", () => {
    const target = targetWith({ layout: ABC, focusedKey: "a" });
    moveSplitPane(target, { key: "c", targetKey: "a", side: "left" }, null);
    expect(target.read()).toEqual({
      layout: moveTerminalPane(ABC, "c", "a", "left"),
      focusedKey: "c",
    });
  });

  it("moves nothing to a spot the split no longer offers, or the size rule refuses", () => {
    const target = targetWith({ layout: AB, focusedKey: "a" });
    const before = target.read();
    // c closed since the spot was read; b right of a changes nothing.
    moveSplitPane(target, { key: "c", targetKey: "a", side: "left" }, null);
    moveSplitPane(target, { key: "b", targetKey: "a", side: "right" }, null);
    moveSplitPane(target, { key: "b", targetKey: "a", side: "left" }, NO_ROOM);
    expect(target.read()).toBe(before);
  });

  it("moves nothing while a pane is maximized", () => {
    const target = targetWith({ layout: ABC, focusedKey: "b" });
    toggleZoomedPane(target);
    const before = target.read();
    moveSplitPane(target, { key: "c", targetKey: "a", side: "left" }, null);
    expect(target.read()).toBe(before);
    expect(zoomedPaneKey(target)).toBe("b");
  });
});

describe("paneDropSpot", () => {
  // b is AB's right half: 50–100% across and the full height, a tall pane in a square view.
  const tall = (across: number, down: number) => ({ x: 50 + across * 50, y: down * 100 });
  // ROWS' lower pane is the whole width and half the height: wide.
  const ROWS: TerminalLayout = { axis: "rows", ratio: 0.5, children: ["a", "b"] };
  const wide = (across: number, down: number) => ({ x: across * 100, y: 50 + down * 50 });
  const sideAt = (layout: TerminalLayout, point: { x: number; y: number }) =>
    paneDropSpot(layout, point)?.side;

  it("picks the pane under the pointer, and the side whose diagonal zone holds it", () => {
    expect(paneDropSpot(AB, { x: 95, y: 50 })).toEqual({ targetKey: "b", side: "right" });
    expect(paneDropSpot(AB, { x: 55, y: 50 })).toEqual({ targetKey: "b", side: "left" });
    expect(paneDropSpot(AB, { x: 85, y: 5 })).toEqual({ targetKey: "b", side: "top" });
    expect(paneDropSpot(ABC, { x: 75, y: 90 })).toEqual({ targetKey: "c", side: "bottom" });
  });

  it.each([
    // [across, down] within the pane, as shares of its width and height.
    [0.5, 0.6, "bottom"],
    [0.4, 0.65, "bottom"],
    [0.6, 0.7, "bottom"],
    [0.5, 0.4, "top"],
    [0.2, 0.6, "left"],
    [0.85, 0.6, "right"],
  ] as const)("in a tall pane, at %s across and %s down, offers %s", (across, down, side) => {
    expect(sideAt(AB, tall(across, down))).toBe(side);
  });

  it.each([
    [0.8, 0.5, "right"],
    [0.2, 0.55, "left"],
    [0.5, 0.75, "bottom"],
    [0.5, 0.25, "top"],
  ] as const)("in a wide pane, at %s across and %s down, offers %s", (across, down, side) => {
    expect(sideAt(ROWS, wide(across, down))).toBe(side);
  });

  it("keeps the side shown while another is only a little nearer, near a diagonal", () => {
    const shown = { targetKey: "b", side: "bottom" } as const;
    // Right is nearer (0.3 against 0.32 away), but not clearly: Bottom stays.
    expect(paneDropSpot(AB, tall(0.7, 0.68), shown)).toEqual(shown);
    expect(sideAt(AB, tall(0.7, 0.68))).toBe("right");
    // Clearly nearer the right: it gives way.
    expect(paneDropSpot(AB, tall(0.85, 0.6), shown)).toEqual({ targetKey: "b", side: "right" });
  });

  it("gives up the side shown once the pointer is over another pane", () => {
    const shown = { targetKey: "a", side: "right" } as const;
    expect(paneDropSpot(AB, tall(0.02, 0.6), shown)).toEqual({ targetKey: "b", side: "left" });
  });

  it("finds nothing outside the panes", () => {
    expect(paneDropSpot(AB, { x: 120, y: 50 })).toBeNull();
    expect(paneDropSpot(AB, { x: 50, y: -1 })).toBeNull();
  });
});

describe("placedPaneArea", () => {
  it("is where a moved pane lands once it has left its place", () => {
    // Moving a above b: a leaves, so b fills the row before the two stack.
    expect(placedPaneArea(AB, { kind: "move", key: "a", targetKey: "b", side: "top" })).toEqual({
      left: 0,
      top: 0,
      width: 100,
      height: 50,
    });
  });

  it("is where an inserted pane lands, the others keeping their proportions", () => {
    const area = placedPaneArea(AB, { kind: "insert", key: "new", targetKey: "b", side: "right" });
    expect(area?.left).toBeCloseTo(200 / 3);
    expect(area?.width).toBeCloseTo(100 / 3);
  });

  it("is null for a placement that changes nothing", () => {
    expect(placedPaneArea(AB, { kind: "move", key: "a", targetKey: "b", side: "left" })).toBeNull();
    expect(
      placedPaneArea(AB, { kind: "insert", key: "a", targetKey: "b", side: "left" }),
    ).toBeNull();
    expect(
      placedPaneArea(AB, { kind: "insert", key: "new", targetKey: "x", side: "left" }),
    ).toBeNull();
  });
});
