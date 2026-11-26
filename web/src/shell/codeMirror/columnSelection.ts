// Column (box) selection: ⌥-drag or ⇧⌥-click selects a rectangle of text, one
// range per line, and the Column select commands grow or shrink it from the keyboard.

import {
  countColumn,
  EditorSelection,
  type EditorState,
  type Extension,
  findColumn,
  StateEffect,
  StateField,
} from "@codemirror/state";
import {
  type Command,
  crosshairCursor,
  EditorView,
  type MouseSelectionStyle,
} from "@codemirror/view";

/** A place in the text by line number and visual column, which may lie past the line's end. */
export interface ColumnPoint {
  line: number;
  column: number;
}

/** A box from the corner where it started to the corner that moves. */
export interface ColumnBox {
  anchor: ColumnPoint;
  head: ColumnPoint;
}

/** How far the pointer moves before an ⌥-press becomes a box rather than an added cursor. */
const DRAG_THRESHOLD_PX = 4;

/**
 * One range per line of `box`, between its two columns, keeping the drag's direction.
 * A line that ends before the box's left edge gets a cursor at its end.
 */
export function columnBoxSelection(state: EditorState, box: ColumnBox): EditorSelection {
  const { anchor, head } = box;
  const firstLine = Math.min(anchor.line, head.line);
  const lastLine = Math.max(anchor.line, head.line);
  const leftColumn = Math.min(anchor.column, head.column);
  const ranges = [];
  for (let number = firstLine; number <= lastLine; number++) {
    const line = state.doc.line(number);
    if (findColumn(line.text, leftColumn, state.tabSize, true) < 0) {
      ranges.push(EditorSelection.cursor(line.to));
      continue;
    }
    const from = findColumn(line.text, anchor.column, state.tabSize);
    const to = findColumn(line.text, head.column, state.tabSize);
    ranges.push(EditorSelection.range(line.from + from, line.from + to));
  }
  const mainIndex = head.line < anchor.line ? 0 : ranges.length - 1;
  return EditorSelection.create(ranges, mainIndex);
}

function pointAt(state: EditorState, position: number): ColumnPoint {
  const line = state.doc.lineAt(position);
  return {
    line: line.number,
    column: countColumn(line.text, state.tabSize, position - line.from),
  };
}

/** The line and column under the pointer, counting columns past a line's end from its width. */
function pointUnder(view: EditorView, event: MouseEvent): ColumnPoint {
  const position = view.posAtCoords({ x: event.clientX, y: event.clientY }, false);
  const point = pointAt(view.state, position);
  const line = view.state.doc.lineAt(position);
  if (position < line.to) return point;
  const lineStart = view.coordsAtPos(line.from);
  if (!lineStart) return point;
  const pastEnd = Math.round((event.clientX - lineStart.left) / view.defaultCharacterWidth);
  return { line: point.line, column: Math.max(point.column, pastEnd) };
}

/**
 * ⌥-drag draws a box from where the press started. A press that doesn't move
 * still adds a cursor, as ⌥-click did before. ⇧⌥ draws the box from the main cursor.
 */
function columnSelectionStyle(view: EditorView, event: MouseEvent): MouseSelectionStyle {
  const startX = event.clientX;
  const startY = event.clientY;
  let startSelection = view.state.selection;
  let anchor = event.shiftKey
    ? pointAt(view.state, startSelection.main.head)
    : pointUnder(view, event);
  let dragged = event.shiftKey;
  return {
    update(update) {
      if (!update.docChanged) return;
      startSelection = startSelection.map(update.changes);
      const lineStart = update.changes.mapPos(update.startState.doc.line(anchor.line).from);
      anchor = { line: update.state.doc.lineAt(lineStart).number, column: anchor.column };
    },
    get(current, _extend, multiple) {
      dragged ||=
        Math.abs(current.clientX - startX) > DRAG_THRESHOLD_PX ||
        Math.abs(current.clientY - startY) > DRAG_THRESHOLD_PX;
      if (!dragged) {
        const cursor = EditorSelection.cursor(view.posAtCoords({ x: startX, y: startY }, false));
        return multiple ? startSelection.addRange(cursor) : EditorSelection.create([cursor]);
      }
      const box = { anchor, head: pointUnder(view, current) };
      const selection = columnBoxSelection(view.state, box);
      drawnBoxes.set(selection, box);
      return selection;
    },
  };
}

const setColumnBox = StateEffect.define<ColumnBox>();

/** The box behind each selection a drag drew, so a Column select command can grow it. */
const drawnBoxes = new WeakMap<EditorSelection, ColumnBox>();

/** The box a drag or the Column select commands last drew, until the selection changes another way. */
const columnBoxField = StateField.define<ColumnBox | null>({
  create: () => null,
  update(box, transaction) {
    for (const effect of transaction.effects) if (effect.is(setColumnBox)) return effect.value;
    if (transaction.docChanged) return null;
    if (transaction.selection) return drawnBoxes.get(transaction.selection) ?? null;
    return box;
  },
});

/** The widest line in the box, in columns, so the box can't grow right past every line. */
function widestColumn(state: EditorState, box: ColumnBox): number {
  let widest = 0;
  const firstLine = Math.min(box.anchor.line, box.head.line);
  const lastLine = Math.max(box.anchor.line, box.head.line);
  for (let number = firstLine; number <= lastLine; number++) {
    const line = state.doc.line(number);
    widest = Math.max(widest, countColumn(line.text, state.tabSize));
  }
  return widest;
}

function moveColumnBox(lines: number, columns: number): Command {
  return (view) => {
    const { state } = view;
    const box = state.field(columnBoxField, false) ?? {
      anchor: pointAt(state, state.selection.main.anchor),
      head: pointAt(state, state.selection.main.head),
    };
    const line = Math.min(Math.max(box.head.line + lines, 1), state.doc.lines);
    let column = Math.max(box.head.column + columns, 0);
    if (columns > 0) {
      column = Math.min(
        column,
        widestColumn(state, { anchor: box.anchor, head: { line, column } }),
      );
    }
    const next = { anchor: box.anchor, head: { line, column } };
    view.dispatch({
      selection: columnBoxSelection(state, next),
      effects: setColumnBox.of(next),
      scrollIntoView: true,
      userEvent: "select",
    });
    return true;
  };
}

export const columnSelectUp = moveColumnBox(-1, 0);
export const columnSelectDown = moveColumnBox(1, 0);
export const columnSelectLeft = moveColumnBox(0, -1);
export const columnSelectRight = moveColumnBox(0, 1);

/** ⌥-drag and ⇧⌥-click box selection, with a crosshair pointer while ⌥ is held. */
export const columnSelection: Extension = [
  columnBoxField,
  EditorView.mouseSelectionStyle.of((view, event) =>
    event.altKey && event.button === 0 && !event.metaKey && !event.ctrlKey
      ? columnSelectionStyle(view, event)
      : null,
  ),
  crosshairCursor(),
];
