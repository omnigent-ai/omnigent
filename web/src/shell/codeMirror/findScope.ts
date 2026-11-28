// Find in selection: the ranges find is limited to while the toggle is on, kept
// in step with edits and tinted, and the reader's own last selection, which the
// toggle takes as the scope. Stepping through matches doesn't replace it.

import {
  type EditorSelection,
  type EditorState,
  type Extension,
  Prec,
  StateEffect,
  StateField,
  type Transaction,
} from "@codemirror/state";
import { getSearchQuery, searchPanelOpen } from "@codemirror/search";
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
} from "@codemirror/view";

/** A run of the document, as offsets. */
export interface FindRange {
  from: number;
  to: number;
}

interface FindScopeState {
  /**
   * The ranges find is limited to, in document order, or null for the whole file. Edits that
   * remove all of it leave it empty, matching nothing, until the toggle turns off.
   */
  scope: readonly FindRange[] | null;
  /** The reader's last non-empty selection, which turning the toggle on takes. */
  candidate: readonly FindRange[] | null;
}

/** Limits find to the given ranges, or to the whole file with null. */
export const setFindScope = StateEffect.define<readonly FindRange[] | null>();

const SCOPE_MARK = Decoration.mark({ class: "cm-find-scope" });
// Wraps find's own match highlights outside the scope, so the stylesheet can hide them there.
const OUTSIDE_SCOPE_MARK = Decoration.mark({ class: "cm-find-outside-scope" });

/** The parts of a document of `length` that `scope`'s sorted ranges leave out. */
function outside(scope: readonly FindRange[], length: number): FindRange[] {
  const gaps: FindRange[] = [];
  let from = 0;
  for (const range of scope) {
    if (range.from > from) gaps.push({ from, to: range.from });
    from = Math.max(from, range.to);
  }
  if (length > from) gaps.push({ from, to: length });
  return gaps;
}

function nonEmptyRanges(selection: EditorSelection): readonly FindRange[] | null {
  const ranges = selection.ranges
    .filter((range) => !range.empty)
    .map((range) => ({ from: range.from, to: range.to }));
  return ranges.length > 0 ? ranges : null;
}

/** `ranges` through `transaction`'s edits, without the ones the edits removed. */
function mapRanges(ranges: readonly FindRange[], transaction: Transaction): readonly FindRange[] {
  if (!transaction.docChanged) return ranges;
  return ranges
    .map((range) => ({
      from: transaction.changes.mapPos(range.from, 1),
      to: transaction.changes.mapPos(range.to, -1),
    }))
    .filter((range) => range.to > range.from);
}

/** The selection the reader chose: kept from clicks and keys, not from stepping through matches. */
function nextCandidate(
  candidate: readonly FindRange[] | null,
  transaction: Transaction,
): readonly FindRange[] | null {
  const remaining = candidate && mapRanges(candidate, transaction);
  const mapped = remaining && remaining.length > 0 ? remaining : null;
  if (!transaction.selection || transaction.isUserEvent("select.search")) return mapped;
  const chosen = nonEmptyRanges(transaction.selection);
  if (chosen) return chosen;
  // Any other collapse lets it go, except a replacement's, which keeps find's place.
  return transaction.isUserEvent("input.replace") ? mapped : null;
}

const findScopeField = StateField.define<FindScopeState>({
  create: (state) => ({ scope: null, candidate: nonEmptyRanges(state.selection) }),
  update(value, transaction) {
    let scope = value.scope && mapRanges(value.scope, transaction);
    for (const effect of transaction.effects) if (effect.is(setFindScope)) scope = effect.value;
    const candidate = nextCandidate(value.candidate, transaction);
    return scope === value.scope && candidate === value.candidate ? value : { scope, candidate };
  },
});

/** The scope range holding `position`, by binary search over the sorted ranges. */
function rangeHolding(scope: readonly FindRange[], position: number): FindRange | null {
  let low = 0;
  let high = scope.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    const range = scope[middle]!;
    if (position < range.from) high = middle - 1;
    else if (position >= range.to) low = middle + 1;
    else return range;
  }
  return null;
}

/** How far past the screen CodeMirror's match highlighting looks for a regular expression. */
const HIGHLIGHT_MARGIN = 250;

/** The visible ranges, joined where they lie close, as CodeMirror's highlighting joins them. */
function mergedVisibleRanges(view: EditorView): FindRange[] {
  const merged: FindRange[] = [];
  for (const { from, to } of view.visibleRanges) {
    const last = merged.at(-1);
    if (last && from - 2 * HIGHLIGHT_MARGIN < last.to) last.to = to;
    else merged.push({ from, to });
  }
  return merged;
}

/**
 * The inside parts of the shown matches that cross a scope edge. Find leaves such a match
 * out, so these join the outside marks, and none of its highlight shows.
 */
function crossingMatchMarks(view: EditorView): DecorationSet {
  const { state } = view;
  const scope = findScopeOf(state);
  const query = getSearchQuery(state);
  if (!scope || scope.length === 0 || !searchPanelOpen(state) || !query.valid || !query.search) {
    return Decoration.none;
  }
  const marks = [];
  // The text on screen plus a margin, as CodeMirror's own match highlighting reads it, so
  // a long line is read near the screen only.
  const margin = query.regexp ? HIGHLIGHT_MARGIN : query.search.length;
  for (const visible of mergedVisibleRanges(view)) {
    const from = Math.max(0, visible.from - margin);
    const to = Math.min(state.doc.length, visible.to + margin);
    const cursor = query.getCursor(state, from, to) as Iterator<FindRange>;
    for (let next = cursor.next(); !next.done; next = cursor.next()) {
      const match = next.value;
      const holder = rangeHolding(scope, match.from);
      if (holder && match.to <= holder.to) continue;
      // The match leaves its range; mark each part of it the scope covers.
      for (let position = match.from; position < match.to;) {
        const range = rangeHolding(scope, position);
        if (!range) {
          position += 1;
          continue;
        }
        const end = Math.min(range.to, match.to);
        marks.push(OUTSIDE_SCOPE_MARK.range(position, end));
        position = end;
      }
    }
  }
  return Decoration.set(marks, true);
}

const crossingMatches = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = crossingMatchMarks(view);
    }
    update(update: ViewUpdate) {
      const changed =
        update.docChanged ||
        update.viewportChanged ||
        findScopeOf(update.state) !== findScopeOf(update.startState) ||
        getSearchQuery(update.state) !== getSearchQuery(update.startState) ||
        searchPanelOpen(update.state) !== searchPanelOpen(update.startState);
      if (changed) this.decorations = crossingMatchMarks(update.view);
    }
  },
  { decorations: (plugin) => plugin.decorations },
);

const scopeDecorations = [
  EditorView.decorations.compute([findScopeField], (state) =>
    Decoration.set(
      (state.field(findScopeField).scope ?? []).map((range) =>
        SCOPE_MARK.range(range.from, range.to),
      ),
    ),
  ),
  // Lower precedence wraps higher, so this mark sits outside the search match spans it covers.
  Prec.lowest(
    EditorView.decorations.compute([findScopeField], (state) => {
      const { scope } = state.field(findScopeField);
      if (!scope) return Decoration.none;
      return Decoration.set(
        outside(scope, state.doc.length).map((range) =>
          OUTSIDE_SCOPE_MARK.range(range.from, range.to),
        ),
      );
    }),
  ),
  Prec.lowest(crossingMatches),
];

/** The ranges find is limited to, or null for the whole file. */
export function findScopeOf(state: EditorState): readonly FindRange[] | null {
  return state.field(findScopeField, false)?.scope ?? null;
}

/** What turning Find in selection on would limit find to, or null without a selection. */
export function findScopeCandidate(state: EditorState): readonly FindRange[] | null {
  return state.field(findScopeField, false)?.candidate ?? null;
}

/** Why Find in selection can't turn on, in its button's tooltip and in ⌘K. */
export const FIND_IN_SELECTION_UNAVAILABLE = "Select text to find in it.";

/** Whether the Find in selection toggle can act: it's on, or there's a selection to turn it on with. */
export function canToggleFindInSelection(state: EditorState): boolean {
  return findScopeOf(state) !== null || findScopeCandidate(state) !== null;
}

/** Turns Find in selection on with the reader's selection, or off; false when there's nothing to scope to. */
export function toggleFindInSelection(view: EditorView, on: boolean): boolean {
  if (!on) {
    if (findScopeOf(view.state) === null) return true;
    view.dispatch({ effects: setFindScope.of(null) });
    return true;
  }
  const candidate = findScopeCandidate(view.state);
  if (!candidate) return false;
  view.dispatch({ effects: setFindScope.of(candidate) });
  return true;
}

export const findScope: Extension = [findScopeField, scopeDecorations];
