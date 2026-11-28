// Indent guides: a thin vertical line at each indent level inside a line's
// leading whitespace, drawn only for the visible lines, with the guide of the
// block around the cursor drawn stronger.

import { getIndentUnit } from "@codemirror/language";
import { type ChangeSet, type EditorState, RangeSetBuilder, type Text } from "@codemirror/state";
import {
  Decoration,
  type DecorationSet,
  type EditorView,
  ViewPlugin,
  type ViewUpdate,
} from "@codemirror/view";
import { indentationOf } from "./codeMirrorFolding";

/** How far a blank line looks for code above and below it to borrow its indentation. */
const BLANK_LINE_LOOKAROUND = 200;
/** How far the cursor's block is followed toward the screen when the cursor is off it. */
const ACTIVE_BLOCK_LOOKAROUND = 2000;

/** A run of whole lines, by line number. */
export interface LineSpan {
  first: number;
  last: number;
}

function rawIndent(state: EditorState, number: number): number | null {
  return indentationOf(state.doc.line(number).text, state.tabSize);
}

/** The nearest code line from `from` in direction `step`, within `limit` lines, or null. */
function nearestCodeLine(
  state: EditorState,
  from: number,
  step: 1 | -1,
  limit = BLANK_LINE_LOOKAROUND,
): { number: number; indent: number } | null {
  for (let distance = 0; distance < limit; distance += 1) {
    const number = from + step * distance;
    if (number < 1 || number > state.doc.lines) return null;
    const indent = rawIndent(state, number);
    if (indent !== null) return { number, indent };
  }
  return null;
}

/**
 * The indentation in columns of lines `first` to `last`. A blank line takes the
 * shallower of the nearest code lines above and below it, so guides run through
 * gaps inside a block but stop where the block ends.
 */
export function lineIndents(state: EditorState, first: number, last: number): number[] {
  const raw: (number | null)[] = [];
  for (let number = first; number <= last; number += 1) raw.push(rawIndent(state, number));
  const below: number[] = Array.from({ length: raw.length });
  let next = nearestCodeLine(state, last + 1, 1)?.indent ?? 0;
  for (let index = raw.length - 1; index >= 0; index -= 1) {
    next = raw[index] ?? next;
    below[index] = next;
  }
  let previous = nearestCodeLine(state, first - 1, -1)?.indent ?? 0;
  return raw.map((indent, index) => {
    if (indent !== null) {
      previous = indent;
      return indent;
    }
    return Math.min(previous, below[index]);
  });
}

/** How many guides a line indented by `indent` columns shows, one per started level. */
export function guideCount(indent: number, unit: number): number {
  return Math.ceil(indent / unit);
}

/** The block around the cursor and the column of its guide, or null at the top level. */
export interface ActiveGuide {
  firstLine: number;
  lastLine: number;
  column: number;
}

/**
 * The code lines indented at least `depth` around `start`, followed through blank
 * lines. It stops at the screen's far edge, since lines past it draw nothing.
 */
function blockAround(state: EditorState, start: number, depth: number, bounds: LineSpan): LineSpan {
  let first = start;
  for (let number = start - 1; number >= bounds.first; number -= 1) {
    const indent = rawIndent(state, number);
    if (indent === null) continue;
    if (indent < depth) break;
    first = number;
  }
  let last = start;
  for (let number = start + 1; number <= bounds.last; number += 1) {
    const indent = rawIndent(state, number);
    if (indent === null) continue;
    if (indent < depth) break;
    last = number;
  }
  return { first, last };
}

/**
 * The cursor's block: the lines indented at least as deep as the cursor's line,
 * or the block it opens when the next code line is deeper, as other code editors do.
 * `visible` is the span on screen; the block is followed no further than that.
 */
export function activeGuide(
  state: EditorState,
  unit: number,
  visible: LineSpan = { first: 1, last: state.doc.lines },
): ActiveGuide | null {
  const cursorLine = state.doc.lineAt(state.selection.main.head).number;
  const cursorIndent = rawIndent(state, cursorLine);
  let start = cursorLine;
  let depth: number;
  if (cursorIndent === null) {
    // A blank line belongs to the shallower of the code around it.
    const above = nearestCodeLine(state, cursorLine - 1, -1);
    const below = nearestCodeLine(state, cursorLine + 1, 1);
    if (!above || !below) return null;
    depth = Math.min(above.indent, below.indent);
    start = depth === above.indent ? above.number : below.number;
  } else {
    depth = cursorIndent;
    const body = nearestCodeLine(state, cursorLine + 1, 1);
    if (body && body.indent > cursorIndent) {
      start = body.number;
      depth = body.indent;
    }
  }
  if (depth === 0) return null;
  const bounds = {
    first: Math.max(Math.min(visible.first, start), start - ACTIVE_BLOCK_LOOKAROUND, 1),
    last: Math.min(Math.max(visible.last, start), start + ACTIVE_BLOCK_LOOKAROUND, state.doc.lines),
  };
  const block = blockAround(state, start, depth, bounds);
  return {
    firstLine: block.first,
    lastLine: block.last,
    column: (guideCount(depth, unit) - 1) * unit,
  };
}

function guideDecoration(count: number, unit: number, activeColumn: number | null): Decoration {
  const style = [`--cm-indent-guides: ${count}`, `--cm-indent-unit: ${unit}ch`];
  if (activeColumn !== null) style.push(`--cm-indent-guide-active: ${activeColumn}ch`);
  return Decoration.line({
    class: activeColumn === null ? "cm-indent-guides" : "cm-indent-guides cm-indent-guide-active",
    attributes: { style: style.join("; ") },
  });
}

/** The guide decorations for the lines in `visible`, in order. */
export function indentGuideDecorations(
  state: EditorState,
  visible: readonly LineSpan[],
): DecorationSet {
  const unit = Math.max(1, getIndentUnit(state));
  const screen = visible.length
    ? { first: visible[0].first, last: visible[visible.length - 1].last }
    : { first: 1, last: 0 };
  const active = visible.length ? activeGuide(state, unit, screen) : null;
  const builder = new RangeSetBuilder<Decoration>();
  const decorations = new Map<string, Decoration>();
  for (const span of visible) {
    const indents = lineIndents(state, span.first, span.last);
    for (let number = span.first; number <= span.last; number += 1) {
      const count = guideCount(indents[number - span.first], unit);
      if (count === 0) continue;
      const isActive = active !== null && number >= active.firstLine && number <= active.lastLine;
      const activeColumn = isActive ? active.column : null;
      const key = `${count}:${activeColumn}`;
      let decoration = decorations.get(key);
      if (!decoration) {
        decoration = guideDecoration(count, unit, activeColumn);
        decorations.set(key, decoration);
      }
      const from = state.doc.line(number).from;
      builder.add(from, from, decoration);
    }
  }
  return builder.finish();
}

function leadingWhitespace(text: string): number {
  return text.length - text.trimStart().length;
}

/**
 * Whether `changes` leave every line's indentation as it was: each edit stays on
 * one code line, after its indentation, as typing in the middle of a line does.
 */
export function keepsIndentation(changes: ChangeSet, before: Text, after: Text): boolean {
  let keeps = true;
  changes.iterChanges((fromA, toA, fromB, toB) => {
    if (!keeps) return;
    const oldLine = before.lineAt(fromA);
    const newLine = after.lineAt(fromB);
    keeps =
      toA <= oldLine.to &&
      toB <= newLine.to &&
      oldLine.text.trim() !== "" &&
      newLine.text.trim() !== "" &&
      fromA - oldLine.from > leadingWhitespace(oldLine.text) &&
      leadingWhitespace(newLine.text) === leadingWhitespace(oldLine.text);
  });
  return keeps;
}

function visibleLines(view: EditorView): LineSpan[] {
  return view.visibleRanges.map((range) => ({
    first: view.state.doc.lineAt(range.from).number,
    last: view.state.doc.lineAt(range.to).number,
  }));
}

function cursorLineOf(state: EditorState): number {
  return state.doc.lineAt(state.selection.main.head).number;
}

function sameSpans(first: readonly LineSpan[], second: readonly LineSpan[]): boolean {
  return (
    first.length === second.length &&
    first.every(
      (span, index) => span.first === second[index].first && span.last === second[index].last,
    )
  );
}

/** Indent guides for the visible lines, at the file's detected indent unit. */
export const indentGuides = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    // CodeMirror reports a viewport change for most edits, so the plugin compares visible lines.
    visible: LineSpan[];

    constructor(view: EditorView) {
      this.visible = visibleLines(view);
      this.decorations = indentGuideDecorations(view.state, this.visible);
    }

    update(update: ViewUpdate) {
      const visible = update.viewportChanged ? visibleLines(update.view) : this.visible;
      const reconfigured = update.transactions.some((transaction) => transaction.reconfigured);
      const structural =
        !sameSpans(visible, this.visible) ||
        reconfigured ||
        (update.docChanged &&
          !keepsIndentation(update.changes, update.startState.doc, update.state.doc));
      const cursorMoved = cursorLineOf(update.startState) !== cursorLineOf(update.state);
      this.visible = visible;
      if (structural || cursorMoved) {
        this.decorations = indentGuideDecorations(update.state, visible);
      } else if (update.docChanged) {
        this.decorations = this.decorations.map(update.changes);
      }
    }
  },
  { decorations: (plugin) => plugin.decorations },
);
