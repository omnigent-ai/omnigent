import { afterEach, describe, expect, it } from "vitest";

import { splitTerminalLayout, type TerminalLayout } from "@/lib/paneSplitLayout";

import { cellPaneSizing, domCellSizing, measurePaneContent, NO_ROOM_MESSAGE } from "./paneSizing";
import { PANE_CHROME, PANE_ROW_PX } from "./paneStrip/paneGeometry";

const COLUMNS: TerminalLayout = { axis: "columns", ratio: 0.5, children: ["left", "right"] };
const ROWS: TerminalLayout = { axis: "rows", ratio: 0.5, children: ["top", "bottom"] };
const FOUR_COLUMNS: TerminalLayout = {
  axis: "columns",
  ratio: 0.5,
  children: [
    { axis: "columns", ratio: 0.5, children: ["a", "b"] },
    { axis: "columns", ratio: 0.5, children: ["c", "d"] },
  ],
};
/** A session terminal's padding (p-3) on each side, and xterm's scrollbar. */
const PADDING = 12;
const SCROLLBAR = 14;

/**
 * The cells a terminal fits in a tile, the way xterm's fit addon counts them, from its parent's
 * whole-pixel size: the tile less row and padding, then the scrollbar.
 */
function fittedCells(
  tile: { width: number; height: number },
  cell: { width: number; height: number },
) {
  const chrome = PANE_CHROME;
  const parentWidth = Math.trunc(tile.width - chrome.x - 2 * PADDING);
  const parentHeight = Math.trunc(tile.height - chrome.y - 2 * PADDING);
  return {
    columns: Math.floor((parentWidth - SCROLLBAR) / cell.width),
    rows: Math.floor(parentHeight / cell.height),
  };
}

const measure = (
  area: { width: number; height: number },
  cell = { width: 8, height: 16 },
  scrollbar = SCROLLBAR,
) => ({ area, inset: { x: 2 * PADDING + scrollbar, y: 2 * PADDING }, cell });

describe("cellPaneSizing", () => {
  it("splits across only where both panes fit 20 columns, at the boundary", () => {
    // Each half: 24 px of padding, 14 of scrollbar and 160 of cells.
    expect(cellPaneSizing(measure({ width: 396, height: 400 })).fits(COLUMNS)).toBe(true);
    expect(cellPaneSizing(measure({ width: 395, height: 400 })).fits(COLUMNS)).toBe(false);
  });

  it("splits down only where both panes fit 6 rows below their strip rows", () => {
    // Each half: 28 px of row, 24 of padding and 96 of cells.
    expect(cellPaneSizing(measure({ width: 600, height: 296 })).fits(ROWS)).toBe(true);
    expect(cellPaneSizing(measure({ width: 600, height: 295 })).fits(ROWS)).toBe(false);
  });

  it.each([
    ["across", COLUMNS],
    ["down", ROWS],
    ["nested", splitTerminalLayout(COLUMNS, "right", "new", "rows")],
    ["stacked", splitTerminalLayout(ROWS, "bottom", "new", "rows")],
  ] as const)("accepts a layout only when every fitted grid is 20 x 6: %s", (_name, layout) => {
    for (const cell of [
      { width: 8, height: 16 },
      { width: 7.2, height: 17 },
      { width: 9.6, height: 19.5 },
    ]) {
      let accepted = 0;
      for (let size = 150; size <= 700; size += 3) {
        const area = { width: size * 1.3, height: size };
        const sizing = cellPaneSizing(measure(area, cell));
        const grids = [...splitPanes(layout, area)].map((tile) => fittedCells(tile, cell));
        const roomy = grids.every((grid) => grid.columns >= 20 && grid.rows >= 6);
        // Whatever the rule accepts really fits; it may refuse a strip row's worth narrower.
        if (sizing.fits(layout)) {
          accepted += 1;
          expect(roomy, `${size} at ${cell.width}`).toBe(true);
        }
      }
      // The largest views fit with room to spare, so a rule that refused everything fails here.
      expect(accepted, `at ${cell.width}`).toBeGreaterThan(0);
    }
  });

  it("refuses undersized splits: padding and borders count on both axes", () => {
    // A 264 px pane fits 13 rows; split down, each 132 px pane fits only 4.
    const tall = {
      area: { width: 600, height: 264 },
      inset: { x: 24, y: 24 },
      cell: { width: 8, height: 16 },
    };
    expect(cellPaneSizing(tall).fits(ROWS)).toBe(false);
    // A 360 px pane fits 42 columns; split across, each 180 px pane fits only 18.
    const wide = {
      area: { width: 360, height: 600 },
      inset: { x: 24, y: 24 },
      cell: { width: 8, height: 16 },
    };
    expect(cellPaneSizing(wide).fits(COLUMNS)).toBe(false);
  });

  it("holds a divider drag to the minimum, letting a too-small pane grow", () => {
    const sizing = cellPaneSizing(measure({ width: 600, height: 400 }));
    expect(sizing.keeps(COLUMNS, { ...COLUMNS, ratio: 0.4 })).toBe(true);
    expect(sizing.keeps(COLUMNS, { ...COLUMNS, ratio: 0.3 })).toBe(false);
    expect(sizing.keeps({ ...COLUMNS, ratio: 0.2 }, { ...COLUMNS, ratio: 0.25 })).toBe(true);
    const from = { ...COLUMNS, ratio: 0.7 };
    expect(sizing.toward(COLUMNS, from)).toBe(from);
    expect(sizing.refusal("columns")).toBe(NO_ROOM_MESSAGE);
    expect(sizing.refusal("rows")).toBe(NO_ROOM_MESSAGE);
  });

  it("keeps a pane's row at its floor even with a tiny font", () => {
    const tiny = measure({ width: 250, height: 400 }, { width: 4, height: 8 });
    // Each 125 px half fits 20 columns at a 4 px cell, but its row can't keep ⋯ and Restore.
    expect(cellPaneSizing(tiny).fits(COLUMNS)).toBe(false);
  });

  it("measures a split pane's row at its tile's width", () => {
    // Each 129 px half fits 20 columns at a 4 px cell, but its row is under the 130 px floor.
    const tiny = measure({ width: 258, height: 400 }, { width: 4, height: 8 });
    expect(cellPaneSizing(tiny).fits(COLUMNS)).toBe(false);
    expect(
      cellPaneSizing(measure({ width: 260, height: 400 }, { width: 4, height: 8 })).fits(COLUMNS),
    ).toBe(true);
  });

  it("keeps the top-left pane's row its floor after the window's controls", () => {
    const corner = { ...measure({ width: 1000, height: 400 }), corner: 196 };
    // The left half's row is 500 px; less 196 for the window's controls it still keeps its floor.
    expect(cellPaneSizing(corner).fits(COLUMNS)).toBe(true);
    // Four 250 px columns: the top-left row keeps only 54 px after the controls.
    expect(cellPaneSizing(corner).fits(FOUR_COLUMNS)).toBe(false);
    expect(cellPaneSizing(measure({ width: 1000, height: 400 })).fits(FOUR_COLUMNS)).toBe(true);
  });
});

/** Each pane's tile in px when `layout` fills `area`. */
function* splitPanes(layout: TerminalLayout, area: { width: number; height: number }) {
  const walk = function* (
    node: TerminalLayout,
    box: { width: number; height: number },
  ): Generator<{ width: number; height: number }> {
    if (typeof node === "string") {
      yield box;
      return;
    }
    const across = node.axis === "columns";
    const first = across
      ? { width: box.width * node.ratio, height: box.height }
      : { width: box.width, height: box.height * node.ratio };
    const second = across
      ? { width: box.width - first.width, height: box.height }
      : { width: box.width, height: box.height - first.height };
    yield* walk(node.children[0], first);
    yield* walk(node.children[1], second);
  };
  yield* walk(layout, area);
}

describe("measurePaneContent", () => {
  afterEach(() => {
    document.body.replaceChildren();
  });

  /** A view holding one pane whose terminal fitted `columns` x `rows` of `cell`. */
  function mountView({
    cell,
    columns = 40,
    rows = 10,
  }: {
    cell: { width: number; height: number };
    columns?: number;
    rows?: number;
  }) {
    const root = document.createElement("div");
    const pane = document.createElement("div");
    pane.dataset.terminalPane = "left";
    const surface = document.createElement("div");
    surface.dataset.terminalCols = String(columns);
    surface.dataset.terminalRows = String(rows);
    surface.dataset.terminalFitReserveWidth = String(SCROLLBAR);
    const grid = document.createElement("div");
    grid.className = "xterm-screen";
    surface.append(grid);
    pane.append(surface);
    root.append(pane);
    document.body.append(root);
    const chrome = PANE_CHROME;
    const tile = { width: 400, height: 300 };
    const rect = (width: number, height: number) => () => ({ width, height }) as DOMRect;
    root.getBoundingClientRect = rect(800, 300);
    pane.getBoundingClientRect = rect(tile.width, tile.height);
    surface.getBoundingClientRect = rect(
      tile.width - chrome.x - 2 * PADDING,
      tile.height - chrome.y - 2 * PADDING,
    );
    grid.getBoundingClientRect = rect(columns * cell.width, rows * cell.height);
    return { root, grid };
  }

  it("finds the padding and scrollbar under a pane's row", () => {
    const { root } = mountView({ cell: { width: 8, height: 16 } });
    expect(measurePaneContent(root)).toEqual({
      area: { width: 800, height: 300 },
      inset: { x: 2 * PADDING + SCROLLBAR, y: 2 * PADDING },
      cell: { width: 8, height: 16 },
      corner: 0,
      topRow: PANE_ROW_PX,
    });
  });

  it("follows a font or zoom change through the drawn grid", () => {
    const { root, grid } = mountView({ cell: { width: 8, height: 16 } });
    expect(domCellSizing(root)?.fits(COLUMNS)).toBe(true);
    grid.getBoundingClientRect = () => ({ width: 40 * 12, height: 10 * 24 }) as DOMRect;
    expect(measurePaneContent(root)?.cell).toEqual({ width: 12, height: 24 });
    // 400 px halves less 24 + 14 leave 362 px: 30 cells at 12 px, 45 at 8.
    expect(domCellSizing(root)?.fits(COLUMNS)).toBe(true);
    expect(domCellSizing(root)?.fits(ROWS)).toBe(false);
  });

  it("reads the window's controls' room from the corner row", () => {
    const { root } = mountView({ cell: { width: 8, height: 16 } });
    const inset = document.createElement("span");
    inset.dataset.paneStripWindowInset = "";
    inset.getBoundingClientRect = () => ({ width: 195.4, height: 28 }) as DOMRect;
    root.querySelector("[data-terminal-pane]")!.prepend(inset);
    expect(measurePaneContent(root)?.corner).toBe(196);
  });

  it("reads the taller row along the Mac window's top, and each pane's own row height", () => {
    const { root } = mountView({ cell: { width: 8, height: 16 } });
    const pane = root.querySelector<HTMLElement>("[data-terminal-pane]")!;
    const strip = document.createElement("div");
    strip.dataset.paneStrip = "a";
    strip.dataset.paneStripTitleBarRow = "";
    strip.getBoundingClientRect = () => ({ width: 400, height: 48 }) as DOMRect;
    pane.prepend(strip);
    const measured = measurePaneContent(root)!;
    expect(measured.topRow).toBe(48);
    // The measured pane's surface sits under its 28 px row, so 20 px more of row isn't inset.
    expect(measured.inset.y).toBe(2 * PADDING - 20);
  });

  it("measures nothing from a hidden pane or before a terminal fits", () => {
    const { root } = mountView({ cell: { width: 8, height: 16 } });
    root.querySelector<HTMLElement>("[data-terminal-pane]")!.dataset.paneHidden = "";
    expect(measurePaneContent(root)).toBeNull();
    expect(domCellSizing(null)).toBeNull();
  });
});
