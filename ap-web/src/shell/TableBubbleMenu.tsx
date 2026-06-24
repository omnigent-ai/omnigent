// Hover-based table handles — row grip (left) and column handle (top).
//
// Row handle — vertical pill to the left of the hovered row.
//   Drag  : mousedown-drag to reorder the row (drop-indicator line shown)
//   Click : dropdown menu — Insert row above/below, Delete row
//
// Column handle — horizontal pill above the hovered column.
//   Drag  : mousedown-drag to reorder the column
//   Click : dropdown menu — Insert column before/after, Delete column
//
// Both are fixed-position portals so they are never clipped by the editor's
// overflow container.  Drag uses mousedown/mousemove/mouseup rather than the
// HTML5 DnD API because ProseMirror intercepts dragover/drop events on its
// own DOM node, making native DnD unreliable.

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { createPortal } from "react-dom";
import type { Editor } from "@tiptap/react";
// Type-only import: activates @tiptap/extension-table's TypeScript module
// augmentation so the editor's table commands resolve, without pulling the
// extension into the runtime bundle.
// eslint-disable-next-line import/no-empty-named-blocks -- deliberate type-only augmentation trigger, not a stray empty import
import type {} from "@tiptap/extension-table";
import { TextSelection } from "@tiptap/pm/state";
import { Fragment } from "@tiptap/pm/model";
import type { Node as PMNode } from "@tiptap/pm/model";
import { MoreHorizontal, MoreVertical, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Public helper — re-derives a cell's document position by DOM index.
// Exported for use in tests.
// ---------------------------------------------------------------------------

export function freshCellPos(editor: Editor, rowIndex: number, colIndex: number): number | null {
  const rows = editor.view.dom.querySelectorAll("tr");
  const row = rows[rowIndex] as HTMLTableRowElement | undefined;
  if (!row) return null;
  const cell = row.cells[colIndex];
  if (!cell) return null;
  try {
    return editor.view.posAtDOM(cell, 0);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// ProseMirror helpers for moving rows / columns
// ---------------------------------------------------------------------------

function getTableContext(editor: Editor, rowIndex: number): { node: PMNode; pos: number } | null {
  const pos = freshCellPos(editor, rowIndex, 0);
  if (pos === null) return null;
  const $pos = editor.state.doc.resolve(pos);
  for (let depth = $pos.depth; depth > 0; depth--) {
    const node = $pos.node(depth);
    if (node.type.name === "table") {
      return { node, pos: $pos.before(depth) };
    }
  }
  return null;
}

// Both helpers are exported so the multi-table isolation tests can exercise them
// directly without going through the React component.

export function moveRowToIndex(editor: Editor, fromIndex: number, toIndex: number): void {
  if (fromIndex === toIndex) return;
  const ctx = getTableContext(editor, fromIndex);
  if (!ctx) return;
  const { node, pos } = ctx;
  const tableStart = getFirstGlobalRowInTable(editor, fromIndex);
  if (tableStart === null) return;
  const localFrom = fromIndex - tableStart;
  const localTo = toIndex - tableStart;
  if (localTo < 0 || localTo >= node.childCount) return;
  const rows = Array.from({ length: node.childCount }, (_, i) => node.child(i));
  const [row] = rows.splice(localFrom, 1);
  rows.splice(localTo, 0, row);
  const newTable = node.type.create(node.attrs, Fragment.fromArray(rows));
  editor.view.dispatch(editor.state.tr.replaceWith(pos, pos + node.nodeSize, newTable));
}

// tableRowIndex: global index of any row that belongs to the target table.
export function moveColumnToIndex(
  editor: Editor,
  fromCol: number,
  toCol: number,
  tableRowIndex: number,
): void {
  if (fromCol === toCol) return;
  const ctx = getTableContext(editor, tableRowIndex);
  if (!ctx) return;
  const { node, pos } = ctx;
  const newRows = Array.from({ length: node.childCount }, (_, r) => {
    const row = node.child(r);
    if (fromCol < 0 || fromCol >= row.childCount) return row;
    if (toCol < 0 || toCol >= row.childCount) return row;
    const cells = Array.from({ length: row.childCount }, (_, c) => row.child(c));
    const [cell] = cells.splice(fromCol, 1);
    cells.splice(toCol, 0, cell);
    return row.type.create(row.attrs, Fragment.fromArray(cells));
  });
  const newTable = node.type.create(node.attrs, Fragment.fromArray(newRows));
  editor.view.dispatch(editor.state.tr.replaceWith(pos, pos + node.nodeSize, newTable));
}

// ---------------------------------------------------------------------------
// Pure coordinate helpers — extracted for unit-testability.
// During drag the cursor often sits outside table cells (e.g. to the left of
// the table when dragging the row handle vertically), so elementFromPoint is
// unreliable.  These helpers find the target by Y / X band instead.
// ---------------------------------------------------------------------------

/**
 * Returns the index (within `rowRects`) of the row whose vertical band
 * contains `y`, or -1 if none.
 */
export function rowIndexAtY(
  rowRects: ReadonlyArray<{ top: number; bottom: number }>,
  y: number,
): number {
  for (let i = 0; i < rowRects.length; i++) {
    if (y >= rowRects[i].top && y < rowRects[i].bottom) return i;
  }
  return -1;
}

/**
 * Returns the `cellIndex` of the cell whose horizontal band contains `x`,
 * or -1 if none.
 */
export function colIndexAtX(
  cellRects: ReadonlyArray<{ left: number; right: number; cellIndex: number }>,
  x: number,
): number {
  for (const r of cellRects) {
    if (x >= r.left && x < r.right) return r.cellIndex;
  }
  return -1;
}

function getFirstGlobalRowInTable(editor: Editor, anyRowIndex: number): number | null {
  const pos = freshCellPos(editor, anyRowIndex, 0);
  if (pos === null) return null;
  const $pos = editor.state.doc.resolve(pos);
  let tablePos: number | null = null;
  for (let depth = $pos.depth; depth > 0; depth--) {
    if ($pos.node(depth).type.name === "table") {
      tablePos = $pos.before(depth);
      break;
    }
  }
  if (tablePos === null) return null;
  const allGlobalRows = Array.from(editor.view.dom.querySelectorAll("tr"));
  for (let i = 0; i < allGlobalRows.length; i++) {
    try {
      const p = editor.view.posAtDOM(allGlobalRows[i], 0);
      const $p = editor.state.doc.resolve(p);
      for (let d = $p.depth; d > 0; d--) {
        if ($p.node(d).type.name === "table" && $p.before(d) === tablePos) {
          return i;
        }
      }
    } catch {
      // skip
    }
  }
  return null;
}

function setCursorToCell(editor: Editor, rowIndex: number, colIndex: number): void {
  const pos = freshCellPos(editor, rowIndex, colIndex);
  if (pos === null) return;
  try {
    // pos + 1 places the cursor inside the cell's first text position;
    // pos alone lands before the cell node, which causes commands to no-op.
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.create(editor.state.doc, pos + 1)),
    );
  } catch {
    // ignore stale positions
  }
}

// ---------------------------------------------------------------------------
// Handle dropdown menu
// ---------------------------------------------------------------------------

type MenuItemDef =
  | { label: string; icon: React.ReactNode; onClick: () => void; destructive?: boolean }
  | { separator: true };

function HandleMenu({
  items,
  anchorTop,
  anchorLeft,
  onClose,
  onMouseEnter,
  onMouseLeave,
}: {
  items: MenuItemDef[];
  anchorTop: number;
  anchorLeft: number;
  onClose: () => void;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
}) {
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (!(e.target instanceof Element)) return;
      if (!e.target.closest("[data-table-handle-menu]")) {
        onClose();
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  return createPortal(
    <div
      data-table-handle-menu
      className="fixed z-[9999] min-w-[180px] overflow-hidden rounded-md border border-border bg-popover py-1 shadow-md"
      style={{ top: anchorTop, left: anchorLeft }}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      {items.map((item, i) =>
        "separator" in item ? (
          <div key={i} className="mx-2 my-1 h-px bg-border" />
        ) : (
          <button
            key={i}
            type="button"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              item.onClick();
              onClose();
            }}
            className={cn(
              "flex w-full items-center gap-2.5 px-3 py-1.5 text-xs transition-colors",
              item.destructive
                ? "text-destructive hover:bg-destructive/10"
                : "text-foreground hover:bg-muted",
            )}
          >
            {item.icon}
            {item.label}
          </button>
        ),
      )}
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// Shared rect type — used for overlays and drop indicator
// ---------------------------------------------------------------------------

interface Rect {
  top: number;
  left: number;
  width: number;
  height: number;
}

// ---------------------------------------------------------------------------
// State types
// ---------------------------------------------------------------------------

interface HandlePos {
  top: number;
  left: number;
  rowIndex: number;
  colIndex: number;
  cellWidth: number;
  rowHeight: number;
  /** Full width of the row (= table width) — used for the row selection overlay. */
  rowWidth: number;
  /** Full height of the table — used for the column selection overlay. */
  tableHeight: number;
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

const DRAG_THRESHOLD = 5; // px — minimum movement before drag mode activates

export function TableHandles({
  editor,
  scrollContainerRef,
}: {
  editor: Editor;
  scrollContainerRef: React.RefObject<HTMLDivElement | null>;
}) {
  const { t } = useTranslation("common");
  const [rowHandle, setRowHandle] = useState<HandlePos | null>(null);
  const [colHandle, setColHandle] = useState<HandlePos | null>(null);
  const [rowMenu, setRowMenu] = useState<{
    anchorTop: number;
    anchorLeft: number;
    handle: HandlePos;
  } | null>(null);
  const [colMenu, setColMenu] = useState<{
    anchorTop: number;
    anchorLeft: number;
    handle: HandlePos;
  } | null>(null);
  // Selection overlays: full-border rectangle around the hovered/dragged row or column.
  const [rowSelRect, setRowSelRect] = useState<Rect | null>(null);
  const [colSelRect, setColSelRect] = useState<Rect | null>(null);
  // Right-click context menu — shown when right-clicking inside any table cell.
  const [tableContextMenu, setTableContextMenu] = useState<{
    x: number;
    y: number;
    rowIndex: number;
    colIndex: number;
  } | null>(null);
  // Target overlay: shown over the row/column the drag is hovering over.
  const [dragTargetRect, setDragTargetRect] = useState<Rect | null>(null);
  // Ghost: semi-transparent copy of the source row/column that follows the cursor.
  const [dragGhostRect, setDragGhostRect] = useState<Rect | null>(null);
  const [dropIndicator, setDropIndicator] = useState<Rect | null>(null);

  const hideTimer = useRef<number>(0);
  const mouseInUI = useRef(false);
  // True while a drag is in progress — prevents hiding the source overlay.
  const isDraggingRef = useRef(false);
  // Set to true by the drag cleanup; checked by onClick to skip menu open.
  const wasDragRef = useRef(false);
  // Holds the teardown function for any in-progress drag so unmount can cancel it.
  const dragCleanupRef = useRef<(() => void) | null>(null);

  // Cancel any in-progress drag when the component unmounts (e.g. route change).
  useEffect(
    () => () => {
      dragCleanupRef.current?.();
    },
    [],
  );

  const scheduleHide = useCallback(() => {
    window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => {
      if (!mouseInUI.current && !isDraggingRef.current) {
        setRowHandle(null);
        setColHandle(null);
        setRowSelRect(null);
        setColSelRect(null);
      }
    }, 150);
  }, []);

  const cancelHide = useCallback(() => {
    window.clearTimeout(hideTimer.current);
  }, []);

  // Track hovered cell to position handles
  useEffect(() => {
    if (!editor.view?.dom) return;
    const dom = editor.view.dom;

    const onMouseMove = (e: MouseEvent) => {
      if (!(e.target instanceof Element)) return;
      const target = e.target;
      const cell = target.closest("td, th");
      const row = target.closest("tr");
      const table = target.closest("table");
      if (!cell || !row || !table) {
        scheduleHide();
        return;
      }
      cancelHide();

      // Use global row index (across all tables) so freshCellPos resolves correctly.
      const allDocRows = Array.from(editor.view.dom.querySelectorAll("tr"));
      const rowIndex = allDocRows.indexOf(row as HTMLTableRowElement);
      const colIndex = (cell as HTMLTableCellElement).cellIndex;
      const rowRect = row.getBoundingClientRect();
      const cellRect = cell.getBoundingClientRect();
      const tableRect = table.getBoundingClientRect();

      // Skip the state update (and consequent re-render) when the cursor is
      // still in the same cell at the same position — mousemove fires very
      // frequently and each setState triggers a portal re-paint.
      setRowHandle((prev) => {
        if (
          prev?.rowIndex === rowIndex &&
          prev.colIndex === colIndex &&
          prev.top === rowRect.top &&
          prev.left === rowRect.left
        )
          return prev;
        return {
          top: rowRect.top,
          left: rowRect.left,
          rowIndex,
          colIndex,
          cellWidth: cellRect.width,
          rowHeight: rowRect.height,
          rowWidth: rowRect.width,
          tableHeight: tableRect.height,
        };
      });
      setColHandle((prev) => {
        if (
          prev?.rowIndex === rowIndex &&
          prev.colIndex === colIndex &&
          prev.top === tableRect.top &&
          prev.left === cellRect.left
        )
          return prev;
        return {
          top: tableRect.top,
          left: cellRect.left,
          rowIndex,
          colIndex,
          cellWidth: cellRect.width,
          rowHeight: rowRect.height,
          rowWidth: rowRect.width,
          tableHeight: tableRect.height,
        };
      });
      // Selection overlays are set only when the handle pill is hovered (see onMouseEnter below).
    };

    const onContextMenu = (e: MouseEvent) => {
      if (!(e.target instanceof Element)) return;
      const target = e.target;
      if (!target.closest("td, th")) return;
      e.preventDefault();
      const cell = target.closest("td, th") as HTMLTableCellElement;
      const row = target.closest("tr") as HTMLTableRowElement | null;
      const table = target.closest("table");
      if (!row || !table) return;
      const allDocRows = Array.from(dom.querySelectorAll("tr"));
      const rowIndex = allDocRows.indexOf(row);
      const colIndex = cell.cellIndex;
      setTableContextMenu({ x: e.clientX, y: e.clientY, rowIndex, colIndex });
    };

    const onMouseLeave = () => scheduleHide();
    const onScroll = () => {
      setRowHandle(null);
      setColHandle(null);
      setRowMenu(null);
      setColMenu(null);
      setRowSelRect(null);
      setColSelRect(null);
      setDragTargetRect(null);
      setDragGhostRect(null);
      setDropIndicator(null);
      setTableContextMenu(null);
    };

    dom.addEventListener("mousemove", onMouseMove);
    dom.addEventListener("mouseleave", onMouseLeave);
    dom.addEventListener("contextmenu", onContextMenu);
    const scrollEl = scrollContainerRef.current;
    scrollEl?.addEventListener("scroll", onScroll);

    return () => {
      dom.removeEventListener("mousemove", onMouseMove);
      dom.removeEventListener("mouseleave", onMouseLeave);
      dom.removeEventListener("contextmenu", onContextMenu);
      scrollEl?.removeEventListener("scroll", onScroll);
      window.clearTimeout(hideTimer.current);
    };
  }, [editor, scheduleHide, cancelHide, scrollContainerRef]);

  // Stretch TipTap's single-cell column-resize-handle to span the full table.
  useEffect(() => {
    if (!editor.view?.dom) return;
    const dom = editor.view.dom;
    // Debounce via rAF so the style writes happen at most once per frame even
    // when a single ProseMirror transaction produces many DOM mutations.
    let rafId = 0;
    const fixResizeHandle = () => {
      cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        const handle = dom.querySelector<HTMLElement>(".column-resize-handle");
        if (!handle) return;
        const cell = handle.closest<HTMLElement>("td, th");
        const table = handle.closest<HTMLElement>("table");
        if (!cell || !table) return;
        const topOffset = cell.getBoundingClientRect().top - table.getBoundingClientRect().top;
        handle.style.top = `-${topOffset}px`;
        handle.style.height = `${table.offsetHeight}px`;
      });
    };
    const observer = new MutationObserver(fixResizeHandle);
    observer.observe(dom, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      cancelAnimationFrame(rafId);
    };
  }, [editor]);

  const handleMouseEnter = () => {
    mouseInUI.current = true;
    cancelHide();
  };
  const handleMouseLeave = () => {
    mouseInUI.current = false;
    scheduleHide();
  };

  // -------------------------------------------------------------------------
  // Mousedown-based drag — avoids ProseMirror HTML5 DnD interference.
  //
  // 1. onMouseDown records start position and attaches document listeners.
  // 2. mousemove: activates drag once past threshold, then highlights target
  //    row/column and shows a drop-indicator line.
  // 3. mouseup: executes the move (or, if no drag, sets wasDragRef=false so
  //    the subsequent onClick can open the menu).
  // -------------------------------------------------------------------------
  const startDrag = useCallback(
    (
      e: React.MouseEvent,
      type: "row" | "col",
      fromIndex: number,
      tableRowIndex: number,
      sourceRect: Rect,
    ) => {
      e.preventDefault(); // prevent editor focus steal on mousedown
      wasDragRef.current = false;

      const startX = e.clientX;
      const startY = e.clientY;
      let dragActive = false;
      let dropTarget: number | null = null;
      const dom = editor.view?.dom;
      if (!dom) return;

      const onMove = (ev: MouseEvent) => {
        if (!dragActive) {
          const dx = ev.clientX - startX;
          const dy = ev.clientY - startY;
          if (Math.sqrt(dx * dx + dy * dy) < DRAG_THRESHOLD) return;
          dragActive = true;
          isDraggingRef.current = true;
          document.body.style.cursor = "grabbing";
        }

        // Ghost follows the cursor: row strip slides vertically, column strip slides horizontally.
        if (type === "row") {
          setDragGhostRect({
            top: ev.clientY - sourceRect.height / 2,
            left: sourceRect.left,
            width: sourceRect.width,
            height: sourceRect.height,
          });
        } else {
          setDragGhostRect({
            top: sourceRect.top,
            left: ev.clientX - sourceRect.width / 2,
            width: sourceRect.width,
            height: sourceRect.height,
          });
        }

        if (type === "row") {
          // Scope hit-testing to the source table's rows only — prevents the
          // ghost/indicator from jumping to rows in other tables.
          const sourceRow = dom.querySelectorAll("tr")[tableRowIndex] as
            | HTMLTableRowElement
            | undefined;
          const sourceTableEl = sourceRow?.closest("table");
          if (!sourceTableEl) return;
          const tableRows = Array.from(sourceTableEl.querySelectorAll("tr"));
          const allDocRows = Array.from(dom.querySelectorAll("tr"));
          const tableStartGlobal = allDocRows.indexOf(tableRows[0] as HTMLTableRowElement);
          const rects = tableRows.map((r) => r.getBoundingClientRect());
          const localIdx = rowIndexAtY(rects, ev.clientY);
          if (localIdx < 0) return;
          const idx = tableStartGlobal + localIdx; // convert back to global index
          if (idx === dropTarget) return;
          dropTarget = idx;

          // Show drag-target overlay (full-border rectangle) over the target row.
          if (idx !== fromIndex) {
            setDragTargetRect({
              top: rects[localIdx].top,
              left: rects[localIdx].left,
              width: rects[localIdx].right - rects[localIdx].left,
              height: rects[localIdx].bottom - rects[localIdx].top,
            });
          } else {
            setDragTargetRect(null);
          }

          // Drop-indicator line above or below the target row.
          const insertAfter = idx > fromIndex;
          setDropIndicator({
            top: insertAfter ? rects[localIdx].bottom - 1 : rects[localIdx].top - 1,
            left: rects[localIdx].left,
            width: rects[localIdx].right - rects[localIdx].left,
            height: 3,
          });
        } else {
          // Look up target column by X band within the source table only.
          const sourceRow = dom.querySelectorAll("tr")[tableRowIndex] as
            | HTMLTableRowElement
            | undefined;
          const tableEl = sourceRow?.closest("table");
          const firstRow = tableEl?.querySelector("tr") as HTMLTableRowElement | null;
          if (!firstRow) return;
          const cellRects = Array.from(firstRow.cells).map((c) => {
            const r = c.getBoundingClientRect();
            return { left: r.left, right: r.right, cellIndex: c.cellIndex };
          });
          const idx = colIndexAtX(cellRects, ev.clientX);
          if (idx < 0 || idx === dropTarget) return;
          dropTarget = idx;

          // Show drag-target overlay over the target column.
          const matched = cellRects.find((r) => r.cellIndex === idx);
          if (matched && tableEl && idx !== fromIndex) {
            const tableRect = tableEl.getBoundingClientRect();
            setDragTargetRect({
              top: tableRect.top,
              left: matched.left,
              width: matched.right - matched.left,
              height: tableRect.height,
            });
          } else {
            setDragTargetRect(null);
          }

          // Drop-indicator line left or right of the target column.
          if (matched && tableEl) {
            const tableRect = tableEl.getBoundingClientRect();
            const insertAfter = idx > fromIndex;
            setDropIndicator({
              top: tableRect.top,
              left: insertAfter ? matched.right - 1 : matched.left - 1,
              width: 3,
              height: tableRect.height,
            });
          }
        }
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        dragCleanupRef.current = null;
        isDraggingRef.current = false;
        setRowSelRect(null);
        setColSelRect(null);
        setDragTargetRect(null);
        setDragGhostRect(null);
        setDropIndicator(null);

        if (dragActive) {
          wasDragRef.current = true; // suppress the upcoming onClick
          if (dropTarget !== null && dropTarget !== fromIndex) {
            if (type === "row") moveRowToIndex(editor, fromIndex, dropTarget);
            else moveColumnToIndex(editor, fromIndex, dropTarget, tableRowIndex);
          }
        }
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      // Store a cancel-only teardown (no document mutation) so that if the
      // component unmounts mid-drag the listeners and body cursor are cleaned
      // up without accidentally applying the row/column move.
      dragCleanupRef.current = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        isDraggingRef.current = false;
        setRowSelRect(null);
        setColSelRect(null);
        setDragTargetRect(null);
        setDragGhostRect(null);
        setDropIndicator(null);
      };
    },
    [editor],
  );

  // Build row menu items (Insert / Delete only — drag handles Move)
  const buildRowItems = (h: HandlePos): MenuItemDef[] => [
    {
      label: t("insertRowAbove"),
      icon: <span className="text-[10px] font-bold">↑</span>,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, 0);
        editor.chain().focus().addRowBefore().run();
      },
    },
    {
      label: t("insertRowBelow"),
      icon: <span className="text-[10px] font-bold">↓</span>,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, 0);
        editor.chain().focus().addRowAfter().run();
      },
    },
    { separator: true },
    {
      label: t("deleteRow"),
      icon: <Trash2 className="size-3.5" />,
      destructive: true,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, 0);
        editor.chain().focus().deleteRow().run();
      },
    },
  ];

  // Build column menu items (Insert / Delete only — drag handles Move)
  const buildColItems = (h: HandlePos): MenuItemDef[] => [
    {
      label: t("insertColumnBefore"),
      icon: <span className="text-[10px] font-bold">←</span>,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, h.colIndex);
        editor.chain().focus().addColumnBefore().run();
      },
    },
    {
      label: t("insertColumnAfter"),
      icon: <span className="text-[10px] font-bold">→</span>,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, h.colIndex);
        editor.chain().focus().addColumnAfter().run();
      },
    },
    { separator: true },
    {
      label: t("deleteColumn"),
      icon: <Trash2 className="size-3.5" />,
      destructive: true,
      onClick: () => {
        const pos = freshCellPos(editor, h.rowIndex, h.colIndex);
        if (pos !== null) {
          editor.view.dispatch(
            editor.state.tr.setSelection(TextSelection.create(editor.state.doc, pos + 1)),
          );
          editor.chain().focus().deleteColumn().run();
        }
      },
    },
  ];

  return (
    <>
      {/* Row selection overlay — full-border rectangle spanning the entire hovered row */}
      {rowSelRect &&
        createPortal(
          <div
            className="pointer-events-none fixed z-40 border-2 border-primary bg-primary/5"
            style={{
              top: rowSelRect.top,
              left: rowSelRect.left,
              width: rowSelRect.width,
              height: rowSelRect.height,
            }}
          />,
          document.body,
        )}

      {/* Column selection overlay — full-border rectangle spanning the entire hovered column */}
      {colSelRect &&
        createPortal(
          <div
            className="pointer-events-none fixed z-40 border-2 border-primary bg-primary/5"
            style={{
              top: colSelRect.top,
              left: colSelRect.left,
              width: colSelRect.width,
              height: colSelRect.height,
            }}
          />,
          document.body,
        )}

      {/* Drag target overlay — shows where the dragged row/column will land */}
      {dragTargetRect &&
        createPortal(
          <div
            className="pointer-events-none fixed z-40 border-2 border-primary/60 bg-primary/10"
            style={{
              top: dragTargetRect.top,
              left: dragTargetRect.left,
              width: dragTargetRect.width,
              height: dragTargetRect.height,
            }}
          />,
          document.body,
        )}

      {/* Row handle — vertical pill with ⋮ to the left of the hovered row */}
      {rowHandle &&
        createPortal(
          <div
            role="button"
            tabIndex={0}
            aria-label={t("rowOptions")}
            className={cn(
              "fixed z-50 flex cursor-grab items-center justify-center rounded-md",
              "border border-primary/30 bg-primary/10 text-primary shadow-sm transition-colors",
              "hover:bg-primary hover:text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
              rowMenu ? "bg-primary text-primary-foreground" : "",
            )}
            style={{
              top: rowHandle.top + 2,
              left: rowHandle.left - 22,
              width: 20,
              height: rowHandle.rowHeight - 4,
            }}
            onMouseEnter={() => {
              mouseInUI.current = true;
              cancelHide();
              setColSelRect(null); // only one overlay active at a time
              setRowSelRect({
                top: rowHandle.top,
                left: rowHandle.left,
                width: rowHandle.rowWidth,
                height: rowHandle.rowHeight,
              });
            }}
            onMouseLeave={() => {
              mouseInUI.current = false;
              if (!isDraggingRef.current) setRowSelRect(null);
              scheduleHide();
            }}
            onMouseDown={(e) =>
              startDrag(e, "row", rowHandle.rowIndex, rowHandle.rowIndex, {
                top: rowHandle.top,
                left: rowHandle.left,
                width: rowHandle.rowWidth,
                height: rowHandle.rowHeight,
              })
            }
            onClick={(e) => {
              if (wasDragRef.current) {
                wasDragRef.current = false;
                return;
              }
              const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
              setRowMenu({ anchorTop: rect.bottom + 4, anchorLeft: rect.left, handle: rowHandle });
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                setRowMenu({
                  anchorTop: rect.bottom + 4,
                  anchorLeft: rect.left,
                  handle: rowHandle,
                });
              }
            }}
          >
            <MoreVertical className="size-3.5" />
          </div>,
          document.body,
        )}

      {rowMenu && (
        <HandleMenu
          items={buildRowItems(rowMenu.handle)}
          anchorTop={rowMenu.anchorTop}
          anchorLeft={rowMenu.anchorLeft}
          onClose={() => setRowMenu(null)}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        />
      )}

      {/* Column handle — horizontal pill with ··· centered above the hovered column */}
      {colHandle &&
        createPortal(
          <div
            role="button"
            tabIndex={0}
            aria-label={t("columnOptions")}
            className={cn(
              "fixed z-50 flex cursor-grab items-center justify-center rounded-md",
              "border border-primary/30 bg-primary/10 text-primary shadow-sm transition-colors",
              "hover:bg-primary hover:text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
              colMenu ? "bg-primary text-primary-foreground" : "",
            )}
            style={{
              top: colHandle.top - 24,
              left: colHandle.left + 2,
              width: colHandle.cellWidth - 4,
              height: 18,
            }}
            onMouseEnter={() => {
              mouseInUI.current = true;
              cancelHide();
              setRowSelRect(null); // only one overlay active at a time
              setColSelRect({
                top: colHandle.top,
                left: colHandle.left,
                width: colHandle.cellWidth,
                height: colHandle.tableHeight,
              });
            }}
            onMouseLeave={() => {
              mouseInUI.current = false;
              if (!isDraggingRef.current) setColSelRect(null);
              scheduleHide();
            }}
            onMouseDown={(e) =>
              startDrag(e, "col", colHandle.colIndex, colHandle.rowIndex, {
                top: colHandle.top,
                left: colHandle.left,
                width: colHandle.cellWidth,
                height: colHandle.tableHeight,
              })
            }
            onClick={(e) => {
              if (wasDragRef.current) {
                wasDragRef.current = false;
                return;
              }
              const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
              setColMenu({ anchorTop: rect.bottom + 4, anchorLeft: rect.left, handle: colHandle });
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                setColMenu({
                  anchorTop: rect.bottom + 4,
                  anchorLeft: rect.left,
                  handle: colHandle,
                });
              }
            }}
          >
            <MoreHorizontal className="size-3.5" />
          </div>,
          document.body,
        )}

      {colMenu && (
        <HandleMenu
          items={buildColItems(colMenu.handle)}
          anchorTop={colMenu.anchorTop}
          anchorLeft={colMenu.anchorLeft}
          onClose={() => setColMenu(null)}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        />
      )}

      {/* Drag ghost — semi-transparent copy of the source row/column that follows the cursor */}
      {dragGhostRect &&
        createPortal(
          <div
            className="pointer-events-none fixed z-[9997] border-2 border-primary bg-primary/15 opacity-90"
            style={{
              top: dragGhostRect.top,
              left: dragGhostRect.left,
              width: dragGhostRect.width,
              height: dragGhostRect.height,
            }}
          />,
          document.body,
        )}

      {/* Right-click context menu — shown when right-clicking inside a table cell */}
      {tableContextMenu && (
        <HandleMenu
          items={[
            {
              label: t("deleteTable"),
              icon: <Trash2 className="size-3.5" />,
              destructive: true,
              onClick: () => {
                setCursorToCell(editor, tableContextMenu.rowIndex, tableContextMenu.colIndex);
                editor.chain().focus().deleteTable().run();
              },
            },
          ]}
          anchorTop={tableContextMenu.y}
          anchorLeft={tableContextMenu.x}
          onClose={() => setTableContextMenu(null)}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        />
      )}

      {/* Drop indicator line shown while dragging */}
      {dropIndicator &&
        createPortal(
          <div
            className="pointer-events-none fixed z-[9999] rounded-sm bg-primary"
            style={{
              top: dropIndicator.top,
              left: dropIndicator.left,
              width: dropIndicator.width,
              height: dropIndicator.height,
            }}
          />,
          document.body,
        )}
    </>
  );
}
