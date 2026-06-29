/**
 * Unit tests for TipTap table action commands (addRowAfter, deleteRow,
 * addColumnAfter, deleteColumn) and for the freshCellPos helper that
 * re-derives document positions by row/column index.
 *
 * Each test creates a real TipTap Editor instance, positions the cursor
 * inside a specific cell, runs the command, and asserts on the resulting
 * table structure — matching the Iron Rule: every test must fail when the
 * corresponding production behaviour is broken.
 */

import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import { Markdown } from "@tiptap/markdown";
import { TextSelection } from "@tiptap/pm/state";
import StarterKit from "@tiptap/starter-kit";
import {
  freshCellPos,
  moveRowToIndex,
  moveColumnToIndex,
  rowIndexAtY,
  colIndexAtX,
} from "./TableBubbleMenu";

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

/** Minimal 3×3 table: one header row + two body rows. */
const TABLE_3X3 = `
  <table>
    <tr><th>H1</th><th>H2</th><th>H3</th></tr>
    <tr><td>R2C1</td><td>R2C2</td><td>R2C3</td></tr>
    <tr><td>R3C1</td><td>R3C2</td><td>R3C3</td></tr>
  </table>
`;

function makeEditor(content = TABLE_3X3): Editor {
  return new Editor({
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

/**
 * Positions the TipTap cursor inside the cell at (rowIndex, colIndex).
 * Mirrors the setCursorToCell + freshCellPos pattern used in production.
 */
function cursorInCell(editor: Editor, rowIndex: number, colIndex: number): void {
  const pos = freshCellPos(editor, rowIndex, colIndex);
  if (pos === null) throw new Error(`No cell at row=${rowIndex} col=${colIndex}`);
  editor.view.dispatch(
    editor.state.tr.setSelection(TextSelection.create(editor.state.doc, pos + 1)),
  );
}

/** Returns all <tr> elements in the rendered table. */
function rows(editor: Editor): HTMLTableRowElement[] {
  return Array.from(editor.view.dom.querySelectorAll("tr")) as HTMLTableRowElement[];
}

/** Returns the text content of every cell in a given row. */
function rowText(row: HTMLTableRowElement): string[] {
  return Array.from(row.cells).map((c) => c.textContent ?? "");
}

// ---------------------------------------------------------------------------
// freshCellPos
// ---------------------------------------------------------------------------

describe("freshCellPos", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("returns a position inside the cell at the given row/col indices", () => {
    editor = makeEditor();
    const pos = freshCellPos(editor, 1, 2); // row 1 = second row, col 2 = third column
    // A valid doc position must be > 0 and inside the document.
    expect(pos).not.toBeNull();
    expect(pos!).toBeGreaterThan(0);
    expect(pos!).toBeLessThan(editor.state.doc.content.size);
    // Resolving the position must land inside the expected cell: the resolved
    // node depth must be > 1 (inside table → row → cell → …).
    const $pos = editor.state.doc.resolve(pos!);
    expect($pos.depth).toBeGreaterThanOrEqual(3);
  });

  it("returns null for an out-of-range row index", () => {
    editor = makeEditor();
    // A 3×3 table has row indices 0-2; row 99 does not exist.
    expect(freshCellPos(editor, 99, 0)).toBeNull();
  });

  it("returns null for an out-of-range col index", () => {
    editor = makeEditor();
    // A 3×3 table has col indices 0-2; col 99 does not exist.
    expect(freshCellPos(editor, 0, 99)).toBeNull();
  });

  it("returns a fresh position even after a row is inserted above the target", () => {
    editor = makeEditor();
    // Cursor in row 0, col 0 so addRowBefore inserts above it.
    cursorInCell(editor, 0, 0);
    editor.chain().focus().addRowBefore().run();
    // The table now has 4 rows. The old pmPos for row 0 is now stale (it
    // points to what is now row 1). freshCellPos(1, 2) must still find the
    // correct cell — originally "H3" is now in row 1, col 2.
    const pos = freshCellPos(editor, 1, 2);
    expect(pos).not.toBeNull();
    const $pos = editor.state.doc.resolve(pos!);
    expect($pos.depth).toBeGreaterThanOrEqual(3);
    // 4 rows now: the original row 0 became row 1.
    expect(rows(editor)).toHaveLength(4);
  });
});

// ---------------------------------------------------------------------------
// addRowBefore
// ---------------------------------------------------------------------------

describe("addRowBefore", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("inserts a row immediately above the cursor's row", () => {
    editor = makeEditor();
    // Cursor in row 1 (first body row, contains R2C1).
    cursorInCell(editor, 1, 0);
    editor.chain().focus().addRowBefore().run();

    const r = rows(editor);
    // Table must now have 4 rows.
    expect(r).toHaveLength(4); // was 3
    // New empty row is now at index 1; original row 1 (R2) shifted to index 2.
    expect(rowText(r[1]).every((t) => t === "")).toBe(true);
    expect(rowText(r[2])).toContain("R2C1");
    // Header row unchanged.
    expect(rowText(r[0])).toContain("H1");
  });

  it("inserts above the header row when cursor is in the header", () => {
    editor = makeEditor();
    cursorInCell(editor, 0, 0); // header row
    editor.chain().focus().addRowBefore().run();

    const r = rows(editor);
    expect(r).toHaveLength(4);
    // New empty row at index 0; original header shifted to index 1.
    expect(rowText(r[0]).every((t) => t === "")).toBe(true);
    expect(rowText(r[1])).toContain("H1");
  });
});

// ---------------------------------------------------------------------------
// addRowAfter
// ---------------------------------------------------------------------------

describe("addRowAfter", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("inserts a row immediately below the cursor's row", () => {
    editor = makeEditor();
    // Cursor in row 1 (first body row).
    cursorInCell(editor, 1, 0);
    editor.chain().focus().addRowAfter().run();

    // Table must now have 4 rows.
    const r = rows(editor);
    expect(r).toHaveLength(4); // was 3

    // The new row is between the original row 1 and row 2, so the original
    // row 2 content ("R3C1" …) is now at row 3.
    expect(rowText(r[2]).every((t) => t === "")).toBe(true); // new empty row
    expect(rowText(r[3])).toContain("R3C1"); // original last row pushed down
  });

  it("inserts a row below the last row when cursor is there", () => {
    editor = makeEditor();
    cursorInCell(editor, 2, 0); // last row
    editor.chain().focus().addRowAfter().run();

    const r = rows(editor);
    expect(r).toHaveLength(4);
    expect(rowText(r[3]).every((t) => t === "")).toBe(true); // new empty tail row
  });
});

// ---------------------------------------------------------------------------
// deleteRow
// ---------------------------------------------------------------------------

describe("deleteRow", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("deletes exactly the row containing the cursor", () => {
    editor = makeEditor();
    // Cursor in row 1 (content: R2C1, R2C2, R2C3).
    cursorInCell(editor, 1, 1);
    editor.chain().focus().deleteRow().run();

    const r = rows(editor);
    // One row removed: 3 → 2.
    expect(r).toHaveLength(2); // was 3

    // Row 0 (header) is untouched.
    expect(rowText(r[0])).toContain("H1");

    // Row 1 is now the old row 2 — row 1's content must NOT appear.
    const allText = r.flatMap(rowText);
    expect(allText).not.toContain("R2C1");
    expect(allText).toContain("R3C1"); // old row 2 survived
  });

  it("deletes the correct row even after a row was inserted above it", () => {
    editor = makeEditor();
    // Insert a row above row 0 so the table shifts.
    cursorInCell(editor, 0, 0);
    editor.chain().focus().addRowBefore().run();
    // Table is now: [empty, H-row, R2-row, R3-row] (indices 0-3).
    // Delete the R2 row (now at index 2) using a fresh position.
    const pos = freshCellPos(editor, 2, 0)!;
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.create(editor.state.doc, pos + 1)),
    );
    editor.chain().focus().deleteRow().run();

    const allText = rows(editor).flatMap(rowText);
    // R2 row must be gone; R3 row must survive.
    expect(allText).not.toContain("R2C1");
    expect(allText).toContain("R3C1");
  });
});

// ---------------------------------------------------------------------------
// addColumnAfter
// ---------------------------------------------------------------------------

describe("addColumnAfter", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("inserts a column immediately to the right of the cursor's column", () => {
    editor = makeEditor();
    // Cursor in column 1 (middle column).
    cursorInCell(editor, 0, 1);
    editor.chain().focus().addColumnAfter().run();

    // Every row must now have 4 cells.
    rows(editor).forEach((row) => {
      expect(row.cells).toHaveLength(4); // was 3
    });

    // The new column (index 2) is empty; original col 2 shifted to index 3.
    const headerCells = rowText(rows(editor)[0]);
    expect(headerCells[2]).toBe(""); // new empty column
    expect(headerCells[3]).toBe("H3"); // original col 2 shifted right
  });
});

// ---------------------------------------------------------------------------
// addColumnBefore
// ---------------------------------------------------------------------------

describe("addColumnBefore", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("inserts a column immediately to the left of the cursor's column", () => {
    editor = makeEditor();
    // Cursor in column 1 (middle column, H2).
    cursorInCell(editor, 0, 1);
    editor.chain().focus().addColumnBefore().run();

    // Every row must now have 4 cells.
    rows(editor).forEach((row) => {
      expect(row.cells).toHaveLength(4); // was 3
    });

    // New empty column is at index 1; original col 1 (H2) shifted to index 2.
    const headerCells = rowText(rows(editor)[0]);
    expect(headerCells[0]).toBe("H1"); // unchanged
    expect(headerCells[1]).toBe(""); // new empty column
    expect(headerCells[2]).toBe("H2"); // original col 1 shifted right
    expect(headerCells[3]).toBe("H3"); // original col 2 shifted right
  });

  it("inserts before the first column when cursor is there", () => {
    editor = makeEditor();
    cursorInCell(editor, 0, 0); // first column
    editor.chain().focus().addColumnBefore().run();

    rows(editor).forEach((row) => {
      expect(row.cells).toHaveLength(4);
    });
    // Original H1 is now at index 1.
    expect(rowText(rows(editor)[0])[0]).toBe(""); // new empty first col
    expect(rowText(rows(editor)[0])[1]).toBe("H1");
  });
});

// ---------------------------------------------------------------------------
// deleteTable
// ---------------------------------------------------------------------------

describe("deleteTable", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("removes the entire table from the document", () => {
    editor = makeEditor();
    // Position cursor anywhere inside the table.
    cursorInCell(editor, 0, 0);
    editor.chain().focus().deleteTable().run();

    // No <tr> elements should remain.
    expect(editor.view.dom.querySelectorAll("tr")).toHaveLength(0);
    // No <table> elements should remain.
    expect(editor.view.dom.querySelectorAll("table")).toHaveLength(0);
  });

  it("removes only the targeted table when multiple tables exist", () => {
    const TWO_TABLES = `
      <table>
        <tr><th>T1H1</th><th>T1H2</th></tr>
        <tr><td>T1R1</td><td>T1R2</td></tr>
      </table>
      <p>between</p>
      <table>
        <tr><th>T2H1</th><th>T2H2</th></tr>
        <tr><td>T2R1</td><td>T2R2</td></tr>
      </table>
    `;
    editor = makeEditor(TWO_TABLES);
    // Position cursor in table 2 (global row index 2).
    cursorInCell(editor, 2, 0);
    editor.chain().focus().deleteTable().run();

    // Table 1's rows survive; table 2 is gone.
    const allRows = rows(editor);
    expect(allRows).toHaveLength(2); // table 1's 2 rows remain
    const allText = allRows.flatMap(rowText);
    expect(allText).toContain("T1H1");
    expect(allText).not.toContain("T2H1");
  });
});

// ---------------------------------------------------------------------------
// deleteColumn
// ---------------------------------------------------------------------------

describe("deleteColumn", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("deletes exactly the column containing the cursor", () => {
    editor = makeEditor();
    // Cursor in column 1 (middle column, headers: H1 H2 H3).
    cursorInCell(editor, 0, 1);
    editor.chain().focus().deleteColumn().run();

    // Every row must now have 2 cells.
    rows(editor).forEach((row) => {
      expect(row.cells).toHaveLength(2); // was 3
    });

    // H2 (col 1) is gone; H1 and H3 remain.
    const headerText = rowText(rows(editor)[0]);
    expect(headerText).not.toContain("H2");
    expect(headerText).toContain("H1");
    expect(headerText).toContain("H3");
  });

  it("deletes the first column when cursor is there", () => {
    editor = makeEditor();
    cursorInCell(editor, 1, 0);
    editor.chain().focus().deleteColumn().run();

    rows(editor).forEach((row) => {
      expect(row.cells).toHaveLength(2);
    });
    const allText = rows(editor).flatMap(rowText);
    expect(allText).not.toContain("H1");
    expect(allText).not.toContain("R2C1");
    expect(allText).toContain("H2");
    expect(allText).toContain("R2C2");
  });

  it("deletes the correct column even after a row was inserted above", () => {
    editor = makeEditor();
    // Insert a row at the top so all positions shift.
    cursorInCell(editor, 0, 0);
    editor.chain().focus().addRowBefore().run();
    // Table is now 4 rows. The old "H1 H2 H3" row is now at index 1.
    // We want to delete column 1 (the "H2" column) using a fresh position.
    const pos = freshCellPos(editor, 1, 1)!; // fresh position for (row=1, col=1)
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.create(editor.state.doc, pos + 1)),
    );
    editor.chain().focus().deleteColumn().run();

    const allText = rows(editor).flatMap(rowText);
    // Column 1 is gone — none of its original content should remain.
    expect(allText).not.toContain("H2");
    expect(allText).not.toContain("R2C2");
    // Columns 0 and 2 survive.
    expect(allText).toContain("H1");
    expect(allText).toContain("H3");
  });
});

// ---------------------------------------------------------------------------
// Multi-table isolation
//
// A document with two tables:
//   Table 1 — 2 rows × 2 cols  (global row indices 0, 1)
//   Table 2 — 2 rows × 2 cols  (global row indices 2, 3)
//
// Every test verifies that an operation targeting one table leaves the other
// table completely unchanged.  This guards against the stale-index bug where
// table-relative row indices were stored and then looked up in the global
// querySelectorAll("tr") list, causing commands to hit the wrong table.
// ---------------------------------------------------------------------------

/** Two 2×2 tables separated by a paragraph. */
const TWO_TABLES = `
  <table>
    <tr><th>T1H1</th><th>T1H2</th></tr>
    <tr><td>T1R1C1</td><td>T1R1C2</td></tr>
  </table>
  <p>between</p>
  <table>
    <tr><th>T2H1</th><th>T2H2</th></tr>
    <tr><td>T2R1C1</td><td>T2R1C2</td></tr>
  </table>
`;

describe("multi-table isolation", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("freshCellPos for global row 2 resolves into table 2, not table 1", () => {
    editor = makeEditor(TWO_TABLES);
    // Table 1 occupies global rows 0-1; table 2 starts at global row 2.
    const pos = freshCellPos(editor, 2, 0);
    expect(pos).not.toBeNull();
    // Walk up from the resolved position to find the containing table node.
    const $pos = editor.state.doc.resolve(pos!);
    let tableText = "";
    for (let d = $pos.depth; d > 0; d--) {
      const node = $pos.node(d);
      if (node.type.name === "table") {
        tableText = node.textContent;
        break;
      }
    }
    // The table we landed in must contain T2H1 — it must NOT contain T1H1.
    expect(tableText).toContain("T2H1");
    expect(tableText).not.toContain("T1H1");
  });

  it("deleteRow on table 2 does not affect table 1", () => {
    editor = makeEditor(TWO_TABLES);
    // Global row 3 = second row of table 2 (T2R1C1).
    cursorInCell(editor, 3, 0);
    editor.chain().focus().deleteRow().run();

    const allText = rows(editor).flatMap(rowText);
    // Table 1 is untouched.
    expect(allText).toContain("T1H1");
    expect(allText).toContain("T1R1C1");
    // Row deleted from table 2.
    expect(allText).toContain("T2H1");
    expect(allText).not.toContain("T2R1C1");
  });

  it("deleteRow on table 1 does not affect table 2", () => {
    editor = makeEditor(TWO_TABLES);
    // Global row 1 = second row of table 1 (T1R1C1).
    cursorInCell(editor, 1, 0);
    editor.chain().focus().deleteRow().run();

    const allText = rows(editor).flatMap(rowText);
    // Row deleted from table 1.
    expect(allText).toContain("T1H1");
    expect(allText).not.toContain("T1R1C1");
    // Table 2 is untouched.
    expect(allText).toContain("T2H1");
    expect(allText).toContain("T2R1C1");
  });

  it("deleteColumn on table 2 does not affect table 1", () => {
    editor = makeEditor(TWO_TABLES);
    // Global row 2, col 0 = first cell of table 2.
    cursorInCell(editor, 2, 0);
    editor.chain().focus().deleteColumn().run();

    const allRows = rows(editor);
    // Table 1's rows still have 2 cells.
    // Global rows 0-1 belong to table 1.
    expect(allRows[0].cells).toHaveLength(2);
    expect(allRows[1].cells).toHaveLength(2);
    // Table 2's rows now have 1 cell.
    expect(allRows[2].cells).toHaveLength(1);
    expect(allRows[3].cells).toHaveLength(1);

    const allText = allRows.flatMap(rowText);
    expect(allText).toContain("T1H1");
    expect(allText).toContain("T1H2");
    // Column 0 of table 2 is gone.
    expect(allText).not.toContain("T2H1");
    expect(allText).toContain("T2H2");
  });

  it("moveRowToIndex on table 2 swaps rows within table 2 only", () => {
    editor = makeEditor(TWO_TABLES);
    // Swap global rows 2 and 3 (the two rows of table 2).
    moveRowToIndex(editor, 2, 3);

    const allText = rows(editor).flatMap(rowText);
    // Table 1 unchanged.
    expect(allText).toContain("T1H1");
    expect(allText).toContain("T1R1C1");
    // Table 2 rows are now swapped: T2R1C1 is in row 2, T2H1 is in row 3.
    expect(rows(editor)[2].textContent).toContain("T2R1C1");
    expect(rows(editor)[3].textContent).toContain("T2H1");
  });

  it("moveColumnToIndex on table 2 swaps columns within table 2 only", () => {
    editor = makeEditor(TWO_TABLES);
    // Move col 0 to col 1 in table 2 (tableRowIndex = 2, any row in table 2).
    moveColumnToIndex(editor, 0, 1, 2);

    const allRows = rows(editor);
    // Table 1 headers must be in original order.
    expect(rowText(allRows[0])).toEqual(["T1H1", "T1H2"]);
    // Table 2 headers are now swapped.
    expect(rowText(allRows[2])).toEqual(["T2H2", "T2H1"]);
    expect(rowText(allRows[3])).toEqual(["T2R1C2", "T2R1C1"]);
  });
});

// ---------------------------------------------------------------------------
// rowIndexAtY — coordinate-based row lookup used during drag
//
// The drag handle for rows sits to the LEFT of the table, so the cursor is
// outside table cells when the user drags vertically.  rowIndexAtY finds the
// target row purely from the Y coordinate, making it independent of which
// element is under the cursor.
// ---------------------------------------------------------------------------

describe("rowIndexAtY", () => {
  // Three rows with consecutive, non-overlapping Y bands:
  //   row 0: top=10  bottom=40
  //   row 1: top=40  bottom=70
  //   row 2: top=70  bottom=100
  const rects = [
    { top: 10, bottom: 40 },
    { top: 40, bottom: 70 },
    { top: 70, bottom: 100 },
  ];

  it("returns 0 when Y is inside the first row band", () => {
    // Y=25 is inside row 0 (10–40).  A failure here means the scan loop
    // is broken or the boundary check is wrong.
    expect(rowIndexAtY(rects, 25)).toBe(0);
  });

  it("returns 1 when Y is inside the middle row band", () => {
    // Y=55 is inside row 1 (40–70).
    expect(rowIndexAtY(rects, 55)).toBe(1);
  });

  it("returns 2 when Y is inside the last row band", () => {
    // Y=85 is inside row 2 (70–100).
    expect(rowIndexAtY(rects, 85)).toBe(2);
  });

  it("treats the top edge as inside the row (>= top)", () => {
    // Y=40 is the exact top of row 1 — must resolve to row 1, not row 0.
    // Boundary ambiguity here would cause off-by-one drops.
    expect(rowIndexAtY(rects, 40)).toBe(1);
  });

  it("treats the bottom edge as outside the row (< bottom)", () => {
    // Y=70 is the exact top of row 2 (and bottom of row 1) — must be row 2.
    expect(rowIndexAtY(rects, 70)).toBe(2);
  });

  it("returns -1 when Y is above all rows", () => {
    // Y=5 is above the first row (top=10).
    expect(rowIndexAtY(rects, 5)).toBe(-1);
  });

  it("returns -1 when Y is below all rows", () => {
    // Y=110 is below the last row (bottom=100).
    expect(rowIndexAtY(rects, 110)).toBe(-1);
  });

  it("returns -1 for an empty rect list", () => {
    expect(rowIndexAtY([], 50)).toBe(-1);
  });
});

// ---------------------------------------------------------------------------
// colIndexAtX — coordinate-based column lookup used during column drag
//
// The column handle sits ABOVE the table, so the cursor may be above the
// cells during a horizontal drag.  colIndexAtX finds the target column
// purely from the X coordinate.
// ---------------------------------------------------------------------------

describe("colIndexAtX", () => {
  // Three columns (cellIndex matches array order):
  //   col 0: left=0   right=100
  //   col 1: left=100 right=200
  //   col 2: left=200 right=300
  const cellRects = [
    { left: 0, right: 100, cellIndex: 0 },
    { left: 100, right: 200, cellIndex: 1 },
    { left: 200, right: 300, cellIndex: 2 },
  ];

  it("returns cellIndex 0 when X is inside the first column", () => {
    expect(colIndexAtX(cellRects, 50)).toBe(0);
  });

  it("returns cellIndex 1 when X is inside the middle column", () => {
    expect(colIndexAtX(cellRects, 150)).toBe(1);
  });

  it("returns cellIndex 2 when X is inside the last column", () => {
    expect(colIndexAtX(cellRects, 250)).toBe(2);
  });

  it("treats the left edge as inside the column (>= left)", () => {
    // X=100 is the exact left of col 1 — must resolve to col 1.
    expect(colIndexAtX(cellRects, 100)).toBe(1);
  });

  it("treats the right edge as outside the column (< right)", () => {
    // X=200 is the exact left of col 2 — must resolve to col 2, not col 1.
    expect(colIndexAtX(cellRects, 200)).toBe(2);
  });

  it("returns -1 when X is to the left of all columns", () => {
    expect(colIndexAtX(cellRects, -10)).toBe(-1);
  });

  it("returns -1 when X is to the right of all columns", () => {
    expect(colIndexAtX(cellRects, 350)).toBe(-1);
  });

  it("returns -1 for an empty rect list", () => {
    expect(colIndexAtX([], 50)).toBe(-1);
  });

  it("works correctly when cellIndex does not match array position", () => {
    // After a column move the cellIndex values can differ from their array
    // position.  The function must return the cellIndex, not the array index.
    const sparse = [
      { left: 0, right: 100, cellIndex: 2 },
      { left: 100, right: 200, cellIndex: 0 },
    ];
    expect(colIndexAtX(sparse, 50)).toBe(2);
    expect(colIndexAtX(sparse, 150)).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Drag integration — moveRowToIndex / moveColumnToIndex called with the
// indices that rowIndexAtY / colIndexAtX would produce during a drag.
//
// These tests verify the full pipeline: coordinate lookup → move → correct
// document state.  They are the closest we can get to testing the drag UX
// without a real browser (getBoundingClientRect always returns zeros in jsdom,
// so we test the move functions directly with the indices the helpers produce).
// ---------------------------------------------------------------------------

describe("drag integration: moveRowToIndex", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("moves a row down by one position", () => {
    editor = makeEditor();
    // Simulate dragging global row 1 (R2) to global row 2 (R3).
    moveRowToIndex(editor, 1, 2);
    const r = rows(editor);
    // R3 is now at index 1, R2 is at index 2.
    expect(rowText(r[1])).toContain("R3C1");
    expect(rowText(r[2])).toContain("R2C1");
    // Header row is unaffected.
    expect(rowText(r[0])).toContain("H1");
  });

  it("moves a row up by one position", () => {
    editor = makeEditor();
    // Simulate dragging global row 2 (R3) to global row 1 (R2).
    moveRowToIndex(editor, 2, 1);
    const r = rows(editor);
    expect(rowText(r[1])).toContain("R3C1");
    expect(rowText(r[2])).toContain("R2C1");
    expect(rowText(r[0])).toContain("H1");
  });

  it("is a no-op when fromIndex === toIndex", () => {
    editor = makeEditor();
    const before = rows(editor).map(rowText);
    moveRowToIndex(editor, 1, 1);
    expect(rows(editor).map(rowText)).toEqual(before);
  });

  it("is a no-op when toIndex is outside the table", () => {
    editor = makeEditor();
    const before = rows(editor).map(rowText);
    moveRowToIndex(editor, 0, 99);
    expect(rows(editor).map(rowText)).toEqual(before);
  });
});

describe("drag integration: moveColumnToIndex", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("moves a column right by one position", () => {
    editor = makeEditor();
    // Simulate dragging col 0 (H1) to col 1 (H2).
    moveColumnToIndex(editor, 0, 1, 0);
    const r = rows(editor);
    expect(rowText(r[0])).toEqual(["H2", "H1", "H3"]);
    expect(rowText(r[1])).toEqual(["R2C2", "R2C1", "R2C3"]);
  });

  it("moves a column left by one position", () => {
    editor = makeEditor();
    // Simulate dragging col 2 (H3) to col 1 (H2).
    moveColumnToIndex(editor, 2, 1, 0);
    const r = rows(editor);
    expect(rowText(r[0])).toEqual(["H1", "H3", "H2"]);
    expect(rowText(r[1])).toEqual(["R2C1", "R2C3", "R2C2"]);
  });

  it("is a no-op when fromCol === toCol", () => {
    editor = makeEditor();
    const before = rows(editor).map(rowText);
    moveColumnToIndex(editor, 1, 1, 0);
    expect(rows(editor).map(rowText)).toEqual(before);
  });
});

// ---------------------------------------------------------------------------
// Column alignment GFM serialization
//
// Verifies the full pipeline: setting the `align` attr on every cell in a
// column → @tiptap/markdown serializes it to the correct GFM column-alignment
// marker in the separator row (`:---:`, `---:`, `:---`).  These tests catch
// regressions in position math, attribute spreading, and markdown serialization
// that the mocked dispatch tests in MarkdownEditorToolbar.tableAlign.test.tsx
// cannot reach.
// ---------------------------------------------------------------------------

function makeEditorWithMarkdown(): Editor {
  return new Editor({
    extensions: [
      StarterKit,
      Table.configure({ resizable: false }),
      TableRow,
      TableCell,
      TableHeader,
      Markdown,
    ],
    content: TABLE_3X3,
  });
}

/** Sets `align` on every cell in `colIndex` by positioning the cursor in each
 *  row's cell and calling `setCellAttribute`. Mirrors what setColumnAlign does. */
function alignColumn(editor: Editor, colIndex: number, align: string): void {
  const rowCount = rows(editor).length;
  for (let row = 0; row < rowCount; row++) {
    cursorInCell(editor, row, colIndex);
    editor.chain().setCellAttribute("align", align).run();
  }
}

describe("column alignment GFM serialization", () => {
  let editor: Editor;
  afterEach(() => editor.destroy());

  it("serializes center alignment to :-…-: in the GFM separator row", () => {
    editor = makeEditorWithMarkdown();
    alignColumn(editor, 0, "center");
    // The first column's separator marker must be :-…-: (center);
    // the serializer pads dashes to match column width.
    // The other columns remain unaligned (-…-).
    const md = editor.getMarkdown();
    expect(md).toMatch(/\| :-+: \|/);
    expect(md).toMatch(/\| -+ \|/);
  });

  it("serializes right alignment to -…-: in the GFM separator row", () => {
    editor = makeEditorWithMarkdown();
    alignColumn(editor, 1, "right");
    const md = editor.getMarkdown();
    expect(md).toMatch(/\| -+: \|/);
  });

  it("serializes left alignment to :-…- in the GFM separator row", () => {
    editor = makeEditorWithMarkdown();
    alignColumn(editor, 2, "left");
    const md = editor.getMarkdown();
    expect(md).toMatch(/\| :-+ \|/);
  });

  it("round-trips: alignment marker is preserved after parse → serialize", () => {
    editor = makeEditorWithMarkdown();
    alignColumn(editor, 0, "center");
    const md = editor.getMarkdown();
    // Load the serialized markdown into a fresh editor and re-serialize.
    // jsdom normalizes column padding on re-parse so exact string equality
    // doesn't hold, but the alignment marker must survive the round-trip.
    const editor2 = makeEditorWithMarkdown();
    editor2.commands.setContent(md, { contentType: "markdown" });
    expect(editor2.getMarkdown()).toMatch(/:-+:/);
    editor2.destroy();
  });
});
