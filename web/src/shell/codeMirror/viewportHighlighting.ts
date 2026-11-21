// Syntax highlighting that tokenizes only what the reader can see plus a
// buffer, so a file of any size stays highlighted without a long main-thread pass.

import { StateEffect, StateField, type Extension, type Text } from "@codemirror/state";
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
} from "@codemirror/view";
import { MAX_HIGHLIGHT_CHARACTERS } from "./shikiDecorations";

/** Files at most this size are tokenized in one pass, which keeps grammar state exact. */
export const WHOLE_DOCUMENT_MAX_LINES = 2_000;
export const WHOLE_DOCUMENT_MAX_CHARACTERS = MAX_HIGHLIGHT_CHARACTERS;

const MINIMUM_BUFFER_LINES = 100;
const VIEWPORT_BUFFER_MULTIPLIER = 2;
const VIEWPORT_DEBOUNCE_MILLISECONDS = 40;

export interface DocumentRange {
  from: number;
  to: number;
}

/** Tokenizes `text`, which starts at document position `from`. */
export type HighlightSlice = (text: string, from: number) => Promise<DecorationSet>;

const setHighlighting = StateEffect.define<DecorationSet>();
const highlighting = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, transaction) {
    for (const effect of transaction.effects) {
      if (effect.is(setHighlighting)) return effect.value;
    }
    return value.map(transaction.changes);
  },
  provide: (field) => EditorView.decorations.from(field),
});

/** Whether `doc` is large enough that only the visible slice is tokenized. */
export function usesViewportHighlighting(doc: Text): boolean {
  return doc.lines > WHOLE_DOCUMENT_MAX_LINES || doc.length > WHOLE_DOCUMENT_MAX_CHARACTERS;
}

/**
 * The slice to tokenize for the visible ranges: the whole document when it is
 * small, otherwise the visible lines plus a buffer, capped at
 * `MAX_HIGHLIGHT_CHARACTERS` around the middle of the view.
 */
export function highlightRange(doc: Text, visibleRanges: readonly DocumentRange[]): DocumentRange {
  if (!usesViewportHighlighting(doc)) return { from: 0, to: doc.length };
  if (visibleRanges.length === 0) {
    return { from: 0, to: Math.min(doc.length, MAX_HIGHLIGHT_CHARACTERS) };
  }
  const visibleFrom = Math.max(
    0,
    Math.min(...visibleRanges.map((range) => range.from), doc.length),
  );
  const visibleTo = Math.max(
    visibleFrom,
    Math.min(Math.max(...visibleRanges.map((range) => range.to), visibleFrom), doc.length),
  );
  const firstLine = doc.lineAt(visibleFrom);
  const lastLine = doc.lineAt(Math.max(visibleFrom, visibleTo - 1));
  const visibleLineCount = lastLine.number - firstLine.number + 1;
  const bufferLines = Math.max(MINIMUM_BUFFER_LINES, visibleLineCount * VIEWPORT_BUFFER_MULTIPLIER);
  const bufferedFrom = doc.line(Math.max(1, firstLine.number - bufferLines)).from;
  const bufferedTo = doc.line(Math.min(doc.lines, lastLine.number + bufferLines)).to;
  if (bufferedTo - bufferedFrom <= MAX_HIGHLIGHT_CHARACTERS) {
    return { from: bufferedFrom, to: bufferedTo };
  }
  const visibleMiddle = visibleFrom + Math.floor((visibleTo - visibleFrom) / 2);
  const from = Math.max(
    0,
    Math.min(
      visibleMiddle - Math.floor(MAX_HIGHLIGHT_CHARACTERS / 2),
      doc.length - MAX_HIGHLIGHT_CHARACTERS,
    ),
  );
  return { from, to: Math.min(doc.length, from + MAX_HIGHLIGHT_CHARACTERS) };
}

function covers(
  visibleRanges: readonly DocumentRange[],
  range: DocumentRange | undefined,
): boolean {
  return (
    range !== undefined &&
    visibleRanges.length > 0 &&
    visibleRanges.every((visible) => visible.from >= range.from && visible.to <= range.to)
  );
}

/**
 * Keeps the visible part of the document highlighted, re-tokenizing after the
 * reader scrolls past the highlighted slice or the document changes.
 */
export function viewportHighlighting(highlightSlice: HighlightSlice): Extension {
  const loader = ViewPlugin.fromClass(
    class {
      private generation = 0;
      private timer: ReturnType<typeof setTimeout> | undefined;
      private destroyed = false;
      private coveredRange: DocumentRange | undefined;
      private pendingRange: DocumentRange | undefined;

      constructor(view: EditorView) {
        this.schedule(view, 0);
      }

      update(update: ViewUpdate): void {
        if (update.docChanged) {
          this.coveredRange = undefined;
          this.schedule(update.view, VIEWPORT_DEBOUNCE_MILLISECONDS);
        } else if (update.viewportChanged) {
          if (covers(update.view.visibleRanges, this.coveredRange)) {
            this.cancelPending();
          } else if (!covers(update.view.visibleRanges, this.pendingRange)) {
            this.schedule(update.view, VIEWPORT_DEBOUNCE_MILLISECONDS);
          }
        }
      }

      destroy(): void {
        this.destroyed = true;
        this.cancelPending();
      }

      private schedule(view: EditorView, delay: number): void {
        const generation = ++this.generation;
        if (this.timer !== undefined) clearTimeout(this.timer);
        const range = highlightRange(view.state.doc, view.visibleRanges);
        this.pendingRange = range;
        this.timer = setTimeout(() => {
          this.timer = undefined;
          void this.load(view, range, generation);
        }, delay);
      }

      private cancelPending(): void {
        this.generation += 1;
        this.pendingRange = undefined;
        if (this.timer !== undefined) clearTimeout(this.timer);
        this.timer = undefined;
      }

      private async load(view: EditorView, range: DocumentRange, generation: number) {
        const text = view.state.doc.sliceString(range.from, range.to);
        let decorations = Decoration.none;
        try {
          decorations = await highlightSlice(text, range.from);
        } catch {
          // Plain text stays fully usable when tokenization fails.
        }
        if (this.destroyed || generation !== this.generation) return;
        this.pendingRange = undefined;
        this.coveredRange = range;
        view.dispatch({ effects: setHighlighting.of(decorations) });
      }
    },
  );
  return [highlighting, loader];
}
