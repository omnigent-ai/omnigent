// Reply quotes inside the CodeMirror composer. Each quote is a token line that paints as a card:
// the caret steps over it, a change can't reach into it, and any edit leaves it on its own line.
// Lazily loaded with the editor; it imports only CodeMirror and the pure document mapping.

import {
  ChangeSet,
  EditorSelection,
  Prec,
  StateField,
  type Annotation,
  type ChangeSpec,
  type EditorState,
  type Extension,
  type Text,
  type Transaction,
  type TransactionSpec,
} from "@codemirror/state";
import { Decoration, EditorView, keymap, WidgetType, type DecorationSet } from "@codemirror/view";
import {
  parseReplyQuoteLine,
  quoteRemovalSpan,
  REPLY_QUOTE_END,
  REPLY_QUOTE_MARK,
  replyQuoteClipboardText,
} from "@/lib/replyDraftDocument";

/** The quote text the card shows before it truncates; the full text stays in its title. */
const QUOTE_PREVIEW_LENGTH = 120;
// The row, card and preview are bounded to the composer's width, so a long quote truncates and
// its X stays on screen.
const ROW_CLASS = "flex w-full min-w-0 max-w-full items-start gap-2 py-1.5";
const CARD_CLASS =
  "min-w-0 max-w-full flex-1 overflow-hidden bg-muted/40 rounded-md border-l-2 border-l-primary/60 px-2 py-1.5 text-sm text-muted-foreground";
const REMOVE_CLASS =
  "mt-0.5 shrink-0 rounded-full text-muted-foreground hover:text-foreground disabled:opacity-60";
const X_ICON =
  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-3.5" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

const TOKEN_PATTERN = new RegExp(`${REPLY_QUOTE_MARK}[A-Za-z0-9_-]*${REPLY_QUOTE_END}`, "g");

interface Span {
  from: number;
  to: number;
}

interface Edit extends Span {
  insert: string;
}

/** Where each token sits in `doc`, in document order. */
function tokenSpans(doc: Text): Span[] {
  const spans: Span[] = [];
  for (let number = 1; number <= doc.lines; number += 1) {
    const line = doc.line(number);
    for (const match of line.text.matchAll(TOKEN_PATTERN)) {
      const from = line.from + match.index;
      spans.push({ from, to: from + match[0].length });
    }
  }
  return spans;
}

/** The change that removes the quote on line `lineNumber`, by the lib's `quoteRemovalSpan`. */
function lineRemoval(doc: Text, lineNumber: number): Edit {
  const lines = Array.from({ length: doc.lines }, (_, index) => doc.line(index + 1).text);
  const { first, last } = quoteRemovalSpan(lines, lineNumber - 1);
  const firstLine = doc.line(first + 1);
  const lastLine = doc.line(last + 1);
  if (last + 1 < doc.lines) return { from: firstLine.from, to: lastLine.to + 1, insert: "" };
  // A last line has no break after it, so the break before the first line goes.
  return { from: Math.max(0, firstLine.from - 1), to: lastLine.to, insert: "" };
}

/** Whether the document holds any quote token, from the decorations the field keeps. */
function hasQuotes(state: EditorState): boolean {
  return state.field(replyQuoteField).size > 0;
}

/** Whether `transaction` inserts a mark, which can only come in as text. */
function insertsMarks(transaction: Transaction): boolean {
  let marks = false;
  transaction.changes.iterChanges((_fromA, _toA, _fromB, _toB, inserted) => {
    const text = inserted.toString();
    if (text.includes(REPLY_QUOTE_MARK) || text.includes(REPLY_QUOTE_END)) marks = true;
  });
  return marks;
}

/**
 * Every annotation of `transaction`. CodeMirror keeps them all at runtime; its typings list only
 * some, so the rest (a kill ring's `killed`, for one) are read directly.
 */
function allAnnotations(transaction: Transaction): readonly Annotation<unknown>[] {
  return (transaction as unknown as { annotations: readonly Annotation<unknown>[] }).annotations;
}

/**
 * The deletions that take quote tokens, and any stray mark, out of inserted text that starts at
 * `offset`. A token's payload goes too, so no encoded text is left behind as plain text.
 */
function markRanges(text: string, offset: number): Edit[] {
  const dropped = new Array<boolean>(text.length).fill(false);
  for (const match of text.matchAll(TOKEN_PATTERN)) {
    dropped.fill(true, match.index, match.index + match[0].length);
  }
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === REPLY_QUOTE_MARK || text[index] === REPLY_QUOTE_END) dropped[index] = true;
  }
  const ranges: Edit[] = [];
  for (let index = 0; index < text.length; index += 1) {
    if (!dropped[index]) continue;
    let end = index;
    while (end < text.length && dropped[end]) end += 1;
    ranges.push({ from: offset + index, to: offset + end, insert: "" });
    index = end;
  }
  return ranges;
}

/**
 * The removal of a whole quote, when `transaction` takes one: the quote's own text with its line,
 * or the break beside it when the line on the other side has text. A blank line beside a quote is
 * just a line, so deleting it leaves the quote alone. The original transaction keeps its
 * annotations and effects; the removal follows it as a sequential spec.
 */
function quoteRemovalOf(transaction: Transaction): TransactionSpec[] | null {
  const edits: Edit[] = [];
  transaction.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
    edits.push({ from: fromA, to: toA, insert: inserted.toString() });
  });
  const [edit] = edits;
  if (edits.length !== 1 || !edit || edit.insert !== "") return null;
  const doc = transaction.startState.doc;
  const breakAt = (from: number) => doc.sliceString(from, from + 1) === "\n";
  for (const span of tokenSpans(doc)) {
    const above = span.from > 0 ? doc.lineAt(span.from - 1).length : 0;
    const below = span.to < doc.length ? doc.lineAt(span.to + 1).length : 0;
    const takesQuote =
      (edit.from === span.from && edit.to === span.to) ||
      (edit.from === span.from && edit.to === span.to + 1 && breakAt(span.to)) ||
      (edit.from === span.from - 1 && edit.to === span.from && breakAt(edit.from) && above > 0) ||
      (edit.from === span.to && edit.to === span.to + 1 && breakAt(span.to) && below > 0);
    if (!takesQuote) continue;
    const line = doc.lineAt(span.from);
    if (line.from !== span.from || line.to !== span.to) return null;
    const removal = lineRemoval(doc, line.number);
    return [
      // The original keeps its annotations and effects, and its change is dropped for the removal.
      {
        changes: [],
        effects: transaction.effects,
        annotations: allAnnotations(transaction),
        scrollIntoView: transaction.scrollIntoView,
      },
      {
        changes: removal,
        selection: EditorSelection.cursor(removal.from + removal.insert.length),
        sequential: true,
      },
    ];
  }
  return null;
}

/**
 * The change filter. A change may cover a whole token or stay clear of it. One that reaches into a
 * token keeps the parts outside it: CodeMirror's filter returns the ranges to keep, so only the
 * part that touches the token is dropped. Without a quote or a mark in play, nothing is scanned.
 */
export function keepsReplyQuotesWhole(transaction: Transaction): boolean | readonly number[] {
  if (
    !transaction.docChanged ||
    transaction.isUserEvent("undo") ||
    transaction.isUserEvent("redo") ||
    (!hasQuotes(transaction.startState) && !insertsMarks(transaction))
  ) {
    return true;
  }
  const spans = tokenSpans(transaction.startState.doc);
  let partial = false;
  transaction.changes.iterChangedRanges((fromA, toA) => {
    for (const span of spans) {
      const overlaps = fromA < span.to && toA > span.from;
      const covers = fromA <= span.from && toA >= span.to;
      if (overlaps && !covers) partial = true;
    }
  });
  if (!partial) return true;
  // CodeMirror drops the changes inside the ranges it's given: the token spans, flattened.
  return spans.flatMap((span) => [span.from, span.to]);
}

/**
 * The transaction filter. A deletion of a whole quote removes it, joining the text around it.
 * Otherwise inserted text loses its marks, and each token that has text on its line is set on a
 * line of its own. The original transaction keeps its annotations, effects and selection; those
 * edits follow it as sequential specs.
 */
export function normalizeReplyQuoteTransaction(
  transaction: Transaction,
): TransactionSpec | readonly TransactionSpec[] | Transaction {
  if (
    !transaction.docChanged ||
    transaction.isUserEvent("undo") ||
    transaction.isUserEvent("redo") ||
    (!hasQuotes(transaction.startState) && !insertsMarks(transaction))
  ) {
    return transaction;
  }
  const removal = quoteRemovalOf(transaction);
  if (removal) return removal;

  const strips: Edit[] = [];
  transaction.changes.iterChanges((_fromA, _toA, fromB, _toB, inserted) => {
    strips.push(...markRanges(inserted.toString(), fromB));
  });
  const afterStrip = ChangeSet.of(strips, transaction.newDoc.length).apply(transaction.newDoc);

  const breaks = new Set<number>();
  for (const span of tokenSpans(afterStrip)) {
    const line = afterStrip.lineAt(span.from);
    if (span.from > line.from) breaks.add(span.from);
    if (span.to < line.to) breaks.add(span.to);
  }
  if (strips.length === 0 && breaks.size === 0) return transaction;

  const specs: TransactionSpec[] = [
    {
      changes: transaction.changes,
      selection: transaction.selection,
      effects: transaction.effects,
      annotations: allAnnotations(transaction),
      scrollIntoView: transaction.scrollIntoView,
    },
  ];
  if (strips.length > 0) specs.push({ changes: strips, sequential: true });
  if (breaks.size > 0) {
    const breakEdits: ChangeSpec = [...breaks]
      .sort((a, b) => a - b)
      .map((position) => ({ from: position, to: position, insert: "\n" }));
    specs.push({ changes: breakEdits, sequential: true });
  }
  return specs;
}

/** Removes the quote a click on its card belongs to, by where its line sits in the document. */
function removeQuoteAt(view: EditorView, position: number): void {
  if (view.state.readOnly) return;
  const line = view.state.doc.lineAt(position);
  if (parseReplyQuoteLine(line.text) === null) return;
  // Deleting exactly the token is a whole-quote deletion, which the transaction filter removes.
  view.dispatch({ changes: { from: line.from, to: line.to }, userEvent: "delete" });
}

/** A quote's card, painted over its line. */
class ReplyQuoteWidget extends WidgetType {
  readonly id: string;
  readonly text: string;
  readonly readOnly: boolean;

  constructor(quote: { id: string; text: string }, readOnly: boolean) {
    super();
    this.id = quote.id;
    this.text = quote.text;
    this.readOnly = readOnly;
  }

  override eq(other: ReplyQuoteWidget): boolean {
    return other.id === this.id && other.text === this.text && other.readOnly === this.readOnly;
  }

  override toDOM(view: EditorView): HTMLElement {
    const { text } = this;
    const row = document.createElement("div");
    row.className = ROW_CLASS;
    row.setAttribute("data-testid", "composer-reply-quote");
    const card = document.createElement("blockquote");
    card.className = CARD_CLASS;
    card.title = text;
    const preview = document.createElement("span");
    preview.className = "block min-w-0 truncate";
    preview.setAttribute("aria-hidden", "true");
    preview.textContent =
      text.length > QUOTE_PREVIEW_LENGTH ? `${text.slice(0, QUOTE_PREVIEW_LENGTH)}…` : text;
    // Screen readers get the whole quote, which the truncated preview hides from them.
    const full = document.createElement("span");
    full.className = "sr-only";
    full.textContent = text;
    card.append(preview, full);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = REMOVE_CLASS;
    remove.setAttribute("aria-label", "Remove quote");
    remove.disabled = this.readOnly;
    remove.innerHTML = X_ICON;
    // Keeps the editor's focus and caret where they are.
    remove.addEventListener("mousedown", (event) => event.preventDefault());
    remove.addEventListener("click", () => removeQuoteAt(view, view.posAtDOM(row)));
    row.append(card, remove);
    return row;
  }
}

function quoteDecorations(state: EditorState): DecorationSet {
  const ranges = [];
  for (let number = 1; number <= state.doc.lines; number += 1) {
    const line = state.doc.line(number);
    const quote = parseReplyQuoteLine(line.text);
    if (!quote) continue;
    // Only the token's own line is replaced: its break stays, so the line after a quote is real.
    ranges.push(
      Decoration.replace({
        block: true,
        widget: new ReplyQuoteWidget(quote, state.readOnly),
      }).range(line.from, line.to),
    );
  }
  return Decoration.set(ranges, true);
}

/** Paints each quote line as a card and makes it atomic, so the caret steps over it whole. */
const replyQuoteField = StateField.define<DecorationSet>({
  create: quoteDecorations,
  update: (decorations, transaction) => {
    if (transaction.startState.readOnly !== transaction.state.readOnly) {
      return quoteDecorations(transaction.state);
    }
    if (!transaction.docChanged) return decorations;
    // Without a quote before or after the change, the decorations only move with the text.
    if (decorations.size === 0 && !insertsMarks(transaction)) {
      return decorations.map(transaction.changes);
    }
    return quoteDecorations(transaction.state);
  },
  provide: (field) => [
    EditorView.decorations.from(field),
    EditorView.atomicRanges.of((view) => view.state.field(field)),
  ],
});

/**
 * Left or right past a card, to the text beside it: from the start of the line after a quote to
 * the end of the line above, and from the end of a line to the start of the line after a quote.
 * Returns false where the default step is already right, so ArrowUp and ArrowDown are untouched.
 */
function stepOverQuote(view: EditorView, direction: -1 | 1): boolean {
  const range = view.state.selection.main;
  if (!range.empty) return false;
  const doc = view.state.doc;
  const line = doc.lineAt(range.head);
  const isQuote = (number: number) => parseReplyQuoteLine(doc.line(number).text) !== null;
  let target: number;
  if (direction < 0) {
    if (range.head !== line.from || line.number === 1 || !isQuote(line.number - 1)) return false;
    let number = line.number - 1;
    while (number >= 1 && isQuote(number)) number -= 1;
    if (number < 1) return false;
    target = doc.line(number).to;
  } else {
    if (range.head !== line.to || line.number === doc.lines || !isQuote(line.number + 1)) {
      return false;
    }
    let number = line.number + 1;
    while (number <= doc.lines && isQuote(number)) number += 1;
    if (number > doc.lines) return false;
    target = doc.line(number).from;
  }
  view.dispatch({ selection: { anchor: target }, scrollIntoView: true });
  return true;
}

const replyQuoteKeys = Prec.highest(
  keymap.of([
    { key: "ArrowLeft", run: (view) => stepOverQuote(view, -1) },
    { key: "ArrowRight", run: (view) => stepOverQuote(view, 1) },
  ]),
);

/** Quote cards and their atomic caret, plus the clipboard's `> ` markdown for copy, cut and drag. */
export const replyQuoteExtensions: Extension = [
  replyQuoteField,
  replyQuoteKeys,
  EditorView.clipboardOutputFilter.of((text) => replyQuoteClipboardText(text)),
];
