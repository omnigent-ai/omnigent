// Tests for the <TableHandles> React component (TableBubbleMenu.tsx) — the
// hover-revealed row/column grips and the menus they open. The pure
// position/move helpers (freshCellPos, moveRowToIndex, …) are covered in
// tableActions.test.ts; this file exercises the *component wiring*:
//
//   - a mousemove over a cell portals the row + column grips into <body>
//     (or the embed root when embedded),
//   - clicking a grip opens its dropdown menu,
//   - clicking each menu item runs the matching TipTap command against the
//     cell the grip belongs to (Insert row above/below, Delete row; Insert
//     column before/after, Delete column),
//   - right-clicking a cell opens the "Delete table" context menu,
//   - scrolling a wide table inside the editor hides the grips,
//   - selection and drag overlays stay within the visible part of a wide table.
//
// A real headless TipTap Editor (with a table) backs the component so the
// assertions are end-to-end on the resulting table structure — each test
// fails if the corresponding command is mis-wired. The editor is mounted into
// a real container and the component's <body> portals are queried directly.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import { StarterKit } from "@tiptap/starter-kit";
import { setEmbedRoot } from "@/lib/host";
import { TableHandles } from "./TableBubbleMenu";

/** Minimal 3×3 table: one header row + two body rows. */
const TABLE_3X3 = `
  <table>
    <tr><th>H1</th><th>H2</th><th>H3</th></tr>
    <tr><td>R2C1</td><td>R2C2</td><td>R2C3</td></tr>
    <tr><td>R3C1</td><td>R3C2</td><td>R3C3</td></tr>
  </table>
`;

const CELL_WIDTH = 100;
const ROW_HEIGHT = 30;
const TABLE_ORIGIN = 40;

let editor: Editor;

/** jsdom has no layout; lay tables out as a grid so the grips have a visible table to sit beside. */
function stubTableLayout() {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const isWrapper = this.classList.contains("tableWrapper");
    const table = isWrapper ? this.querySelector("table") : this.closest("table");
    if (!table) return new DOMRect(0, 0, 0, 0);
    const width = (table.rows[0]?.cells.length ?? 0) * CELL_WIDTH;
    if (this instanceof HTMLTableCellElement) {
      const row = this.parentElement as HTMLTableRowElement;
      return new DOMRect(
        TABLE_ORIGIN + this.cellIndex * CELL_WIDTH,
        TABLE_ORIGIN + row.rowIndex * ROW_HEIGHT,
        CELL_WIDTH,
        ROW_HEIGHT,
      );
    }
    if (this instanceof HTMLTableRowElement) {
      return new DOMRect(
        TABLE_ORIGIN,
        TABLE_ORIGIN + this.rowIndex * ROW_HEIGHT,
        width,
        ROW_HEIGHT,
      );
    }
    if (this === table || isWrapper) {
      return new DOMRect(TABLE_ORIGIN, TABLE_ORIGIN, width, table.rows.length * ROW_HEIGHT);
    }
    return new DOMRect(0, 0, 0, 0);
  });
}

function makeEditor(content = TABLE_3X3): Editor {
  const el = document.createElement("div");
  document.body.appendChild(el);
  return new Editor({
    element: el,
    extensions: [
      StarterKit,
      Table.configure({ resizable: false }),
      TableRow,
      TableCell,
      TableHeader,
    ],
    content,
  });
}

function rows(): HTMLTableRowElement[] {
  return Array.from(editor.view.dom.querySelectorAll("tr")) as HTMLTableRowElement[];
}

function rowText(row: HTMLTableRowElement): string[] {
  return Array.from(row.cells).map((c) => c.textContent ?? "");
}

function allCellText(): string[] {
  return rows().flatMap(rowText);
}

function renderHandles() {
  const ref = createRef<HTMLDivElement>();
  return render(<TableHandles editor={editor} scrollContainerRef={ref} />);
}

/** Renders the handles with the editor inside a scroll container laid out at `viewport`. */
function renderInScrollContainer(viewport: DOMRect): HTMLDivElement {
  const scrollContainer = document.createElement("div");
  document.body.appendChild(scrollContainer);
  onTestFinished(() => scrollContainer.remove());
  scrollContainer.getBoundingClientRect = () => viewport;
  scrollContainer.appendChild(editor.view.dom);
  render(<TableHandles editor={editor} scrollContainerRef={{ current: scrollContainer }} />);
  return scrollContainer;
}

/** Hovers the cell at (rowIndex, colIndex), revealing the grips. */
function hoverCell(rowIndex: number, colIndex: number): HTMLTableCellElement {
  const cell = rows()[rowIndex].cells[colIndex];
  // mousemove must bubble from a real cell so the component's closest()/indexOf
  // logic resolves the row + column indices.
  fireEvent.mouseMove(cell, { bubbles: true });
  return cell;
}

/** Opens the row grip menu over (rowIndex, colIndex) and returns its items. */
function openRowMenu(rowIndex: number, colIndex = 0): HTMLElement {
  hoverCell(rowIndex, colIndex);
  fireEvent.click(screen.getByRole("button", { name: "Row options" }));
  return screen.getByText("Delete row").closest("[data-table-handle-menu]") as HTMLElement;
}

/** Opens the column grip menu over (rowIndex, colIndex) and returns its items. */
function openColMenu(rowIndex: number, colIndex: number): HTMLElement {
  hoverCell(rowIndex, colIndex);
  fireEvent.click(screen.getByRole("button", { name: "Column options" }));
  return screen.getByText("Delete column").closest("[data-table-handle-menu]") as HTMLElement;
}

beforeEach(() => {
  stubTableLayout();
  editor = makeEditor();
});

afterEach(() => {
  cleanup();
  editor.destroy();
  setEmbedRoot(null);
  vi.restoreAllMocks();
});

describe("TableHandles — grip visibility", () => {
  it("reveals the row and column grips when a table cell is hovered", () => {
    // WHY: the grips are portaled in only on hover; before any mousemove the
    // toolbar must be absent, and a mousemove over a cell must reveal both.
    renderHandles();
    expect(screen.queryByRole("button", { name: "Row options" })).toBeNull();
    hoverCell(1, 1);
    expect(screen.getByRole("button", { name: "Row options" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Column options" })).toBeInTheDocument();
  });

  it("opens the row menu on grip click and the column menu on its grip click", () => {
    // WHY: clicking each grip must open exactly its own dropdown with the
    // expected insert/delete items.
    renderHandles();
    const rowMenu = openRowMenu(1);
    expect(within(rowMenu).getByText("Insert row above")).toBeInTheDocument();
    expect(within(rowMenu).getByText("Insert row below")).toBeInTheDocument();
    expect(within(rowMenu).getByText("Delete row")).toBeInTheDocument();

    const colMenu = openColMenu(1, 1);
    expect(within(colMenu).getByText("Insert column before")).toBeInTheDocument();
    expect(within(colMenu).getByText("Insert column after")).toBeInTheDocument();
    expect(within(colMenu).getByText("Delete column")).toBeInTheDocument();
  });

  it("hides the grips and menu when a wide table scrolls inside the editor", () => {
    // WHY: a wide table scrolls its own wrapper, and that scroll event doesn't
    // bubble; grips and menus left behind point at cells that have moved.
    renderInScrollContainer(new DOMRect(0, 0, 800, 600));
    openRowMenu(1, 1);
    expect(screen.getByRole("button", { name: "Column options" })).toBeInTheDocument();
    fireEvent.scroll(editor.view.dom.querySelector(".tableWrapper")!);
    expect(screen.queryByRole("button", { name: "Row options" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Column options" })).toBeNull();
    expect(screen.queryByText("Delete row")).toBeNull();
  });

  it("keeps the grips and menu when another element inside the editor scrolls", () => {
    // WHY: scrolling a wide code block doesn't move the table, so it must not
    // close an open grip menu.
    const scrollContainer = renderInScrollContainer(new DOMRect(0, 0, 800, 600));
    const codeBlock = document.createElement("pre");
    scrollContainer.appendChild(codeBlock);
    openRowMenu(1);
    fireEvent.scroll(codeBlock);
    expect(screen.getByRole("button", { name: "Row options" })).toBeInTheDocument();
    expect(screen.getByText("Delete row")).toBeInTheDocument();
  });
});

describe("TableHandles — overlays", () => {
  it("clips the selection and drag overlays to the visible part of a wide table", () => {
    // WHY: the overlays are fixed portals, so only clipping keeps them from
    // being drawn past the editor's edge, over the rest of the page.
    const viewportRight = 200;
    renderInScrollContainer(new DOMRect(0, 0, viewportRight, 600));
    hoverCell(1, 1);
    const rowGrip = screen.getByRole("button", { name: "Row options" });
    fireEvent.mouseEnter(rowGrip);
    fireEvent.mouseDown(rowGrip, { clientX: 20, clientY: 85 });
    fireEvent.mouseMove(document, { clientX: 20, clientY: 115 });

    const overlays = Array.from(
      document.querySelectorAll<HTMLElement>("div.pointer-events-none.fixed"),
    );
    // Row selection, drag ghost, drop target and drop indicator.
    expect(overlays).toHaveLength(4);
    for (const overlay of overlays) {
      const left = parseFloat(overlay.style.left);
      expect(left).toBeGreaterThanOrEqual(TABLE_ORIGIN);
      expect(left + parseFloat(overlay.style.width)).toBeLessThanOrEqual(viewportRight);
    }
    fireEvent.mouseUp(document, { clientX: 20, clientY: 115 });
  });

  it("updates the row selection when the hovered row grows under a still grip", () => {
    // WHY: typing or a column resize can widen the row without moving its
    // grip; the selection must follow the row, not its earlier size.
    renderInScrollContainer(new DOMRect(0, 0, 800, 600));
    const wrapper = editor.view.dom.querySelector<HTMLElement>(".tableWrapper")!;
    wrapper.getBoundingClientRect = () => new DOMRect(TABLE_ORIGIN, TABLE_ORIGIN, 700, 90);
    hoverCell(1, 1);
    rows()[1].getBoundingClientRect = () =>
      new DOMRect(TABLE_ORIGIN, TABLE_ORIGIN + ROW_HEIGHT, 500, ROW_HEIGHT);
    hoverCell(1, 1);
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Row options" }));
    const selection = document.querySelector<HTMLElement>("div.pointer-events-none.fixed");
    expect(selection?.style.width).toBe("500px");
  });
});

describe("TableHandles — row menu commands", () => {
  it("Insert row above adds an empty row above the gripped row", () => {
    // WHY: the menu must run addRowBefore against the gripped row — inserting
    // an empty row at that index and shifting the original down.
    renderHandles();
    const menu = openRowMenu(1);
    fireEvent.click(within(menu).getByText("Insert row above"));
    const r = rows();
    expect(r).toHaveLength(4);
    expect(rowText(r[1]).every((t) => t === "")).toBe(true);
    expect(rowText(r[2])).toContain("R2C1");
  });

  it("Insert row below adds an empty row beneath the gripped row", () => {
    // WHY: addRowAfter on row 1 must drop a fresh row at index 2, pushing the
    // original row 2 (R3C1) down to index 3.
    renderHandles();
    const menu = openRowMenu(1);
    fireEvent.click(within(menu).getByText("Insert row below"));
    const r = rows();
    expect(r).toHaveLength(4);
    expect(rowText(r[2]).every((t) => t === "")).toBe(true);
    expect(rowText(r[3])).toContain("R3C1");
  });

  it("Delete row removes exactly the gripped row", () => {
    // WHY: deleteRow must drop the gripped row (R2…) and keep header + R3.
    renderHandles();
    const menu = openRowMenu(1);
    fireEvent.click(within(menu).getByText("Delete row"));
    expect(rows()).toHaveLength(2);
    expect(allCellText()).not.toContain("R2C1");
    expect(allCellText()).toContain("R3C1");
  });

  it("closes the menu after an item is chosen", () => {
    // WHY: selecting an item must dismiss the dropdown (onClose), not leave a
    // stale menu portal behind.
    renderHandles();
    const menu = openRowMenu(1);
    fireEvent.click(within(menu).getByText("Insert row above"));
    expect(screen.queryByText("Delete row")).toBeNull();
  });
});

describe("TableHandles — column menu commands", () => {
  it("Insert column before adds an empty column at the gripped column", () => {
    // WHY: addColumnBefore on column 1 must widen every row to 4 cells and put
    // a blank cell at index 1.
    renderHandles();
    const menu = openColMenu(0, 1);
    fireEvent.click(within(menu).getByText("Insert column before"));
    rows().forEach((r) => expect(r.cells).toHaveLength(4));
    expect(rows()[0].cells[1].textContent).toBe("");
  });

  it("Insert column after adds an empty column past the gripped column", () => {
    // WHY: addColumnAfter on column 1 widens to 4 cells with the blank at
    // index 2, leaving the original column-1 header (H2) in place.
    renderHandles();
    const menu = openColMenu(0, 1);
    fireEvent.click(within(menu).getByText("Insert column after"));
    rows().forEach((r) => expect(r.cells).toHaveLength(4));
    expect(rows()[0].cells[1].textContent).toBe("H2");
    expect(rows()[0].cells[2].textContent).toBe("");
  });

  it("Delete column removes exactly the gripped column", () => {
    // WHY: deleteColumn on column 1 must narrow every row to 2 cells and drop
    // the middle column's content (H2 / R2C2 / R3C2).
    renderHandles();
    const menu = openColMenu(0, 1);
    fireEvent.click(within(menu).getByText("Delete column"));
    rows().forEach((r) => expect(r.cells).toHaveLength(2));
    expect(allCellText()).not.toContain("H2");
    expect(allCellText()).not.toContain("R2C2");
  });
});

describe("TableHandles — context menu", () => {
  it("right-clicking a cell opens a Delete table item that removes the table", () => {
    // WHY: the cell contextmenu must surface a "Delete table" action whose
    // click runs deleteTable, leaving no <table> behind.
    renderHandles();
    fireEvent.contextMenu(rows()[1].cells[1], { bubbles: true, clientX: 10, clientY: 10 });
    const item = screen.getByText("Delete table");
    fireEvent.click(item);
    expect(editor.view.dom.querySelector("table")).toBeNull();
  });
});

describe("TableHandles — embedded portal target", () => {
  it("portals grips, menus and overlays into the embed root", () => {
    // WHY: managed CSS is scoped under the embed root; body-level portals
    // render unstyled, so the table controls become invisible and unusable.
    const embedRoot = document.createElement("div");
    document.body.appendChild(embedRoot);
    onTestFinished(() => embedRoot.remove());
    setEmbedRoot(embedRoot);
    renderHandles();
    // Any portal into <body> would add a direct child beyond these.
    const bodyChildren = Array.from(document.body.children);

    const rowMenu = openRowMenu(1);
    expect(embedRoot).toContainElement(screen.getByRole("button", { name: "Column options" }));
    expect(embedRoot).toContainElement(rowMenu);

    // Hovering the grip shows the selection overlay; dragging it shows the
    // ghost, target and drop-indicator overlays.
    const rowGrip = screen.getByRole("button", { name: "Row options" });
    expect(embedRoot).toContainElement(rowGrip);
    fireEvent.mouseEnter(rowGrip);
    fireEvent.mouseDown(rowGrip, { clientX: 0, clientY: 0 });
    fireEvent.mouseMove(document, { clientX: 0, clientY: 40 });
    expect(Array.from(document.body.children)).toEqual(bodyChildren);
    expect(embedRoot.childElementCount).toBeGreaterThan(3);
    fireEvent.mouseUp(document, { clientX: 0, clientY: 40 });

    fireEvent.contextMenu(rows()[1].cells[1], { bubbles: true, clientX: 10, clientY: 10 });
    expect(embedRoot).toContainElement(screen.getByText("Delete table"));
    expect(Array.from(document.body.children)).toEqual(bodyChildren);
  });
});
