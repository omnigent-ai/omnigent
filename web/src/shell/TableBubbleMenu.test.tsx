// Tests for the <TableHandles> React component (TableBubbleMenu.tsx) — the
// hover-revealed row/column grips and the menus they open. The pure
// position/move helpers (freshCellPos, moveRowToIndex, …) are covered in
// tableActions.test.ts; this file exercises the *component wiring*:
//
//   - a mousemove over a cell portals the row + column grips into <body>,
//   - clicking a grip opens its dropdown menu,
//   - clicking each menu item runs the matching TipTap command against the
//     cell the grip belongs to (Insert row above/below, Delete row; Insert
//     column before/after, Delete column),
//   - right-clicking a cell opens the "Delete table" context menu.
//
// A real headless TipTap Editor (with a table) backs the component so the
// assertions are end-to-end on the resulting table structure — each test
// fails if the corresponding command is mis-wired. The editor is mounted into
// a real container and the component's <body> portals are queried directly.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import StarterKit from "@tiptap/starter-kit";
import { TableHandles } from "./TableBubbleMenu";

/** Minimal 3×3 table: one header row + two body rows. */
const TABLE_3X3 = `
  <table>
    <tr><th>H1</th><th>H2</th><th>H3</th></tr>
    <tr><td>R2C1</td><td>R2C2</td><td>R2C3</td></tr>
    <tr><td>R3C1</td><td>R3C2</td><td>R3C3</td></tr>
  </table>
`;

let editor: Editor;

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
  editor = makeEditor();
});

afterEach(() => {
  cleanup();
  editor.destroy();
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
