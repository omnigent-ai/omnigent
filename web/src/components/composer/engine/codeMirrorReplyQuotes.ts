// Reply quotes inside the CodeMirror composer. Each quote is a token line that paints as a card:
// the caret steps over it, a change can't reach into it, and any edit leaves it on its own line.
// Lazily loaded with the editor; it imports only CodeMirror and the pure document mapping.

import { isolateHistory } from "@codemirror/commands";
import {
  ChangeSet,
  EditorSelection,
  StateField,
  Transaction,
  type Annotation,
  type ChangeSpec,
  type EditorState,
  type Extension,
  type Text,
  type TransactionSpec,
} from "@codemirror/state";
import { Decoration, EditorView, WidgetType, type DecorationSet } from "@codemirror/view";
import {
  parseReplyQuoteLine,
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

/**
 * The change that removes the quote on line `lineNumber`, as `removeReplyQuoteLine` does: the same
 * lines go, and nothing else is touched.
 */
function lineRemoval(doc: Text, lineNumber: number): Edit {
  const textOf = (number: number) =>
    number >= 1 && number <= doc.lines ? doc.line(number).text : undefined;
  const isQuote = (number: number) =>
    textOf(number) !== undefined && parseReplyQuoteLine(textOf(number)!) !== null;
  const line = doc.line(lineNumber);
  const above = textOf(lineNumber - 1);
  const below = textOf(lineNumber + 1);
  const beforeIsBlank = above === "" && (lineNumber < 3 || isQuote(lineNumber - 2));
  const textAbove =
    !beforeIsBlank && above !== undefined && above !== "" && !isQuote(lineNumber - 1);
  if (textAbove && below === "") {
    // The blank line below is the empty tail, or a next quote's empty before: the join absorbs it.
    const blank = doc.line(lineNumber + 1);
    if (lineNumber + 1 === doc.lines) {
      return { from: line.from - 1, to: blank.to, insert: "" };
    }
    return { from: line.from, to: blank.to + 1, insert: "" };
  }
  if (textAbove && below !== undefined && below !== "" && !isQuote(lineNumber + 1)) {
    return { from: line.from, to: line.to + 1, insert: "\n" };
  }
  const first = beforeIsBlank ? doc.line(lineNumber - 1) : line;
  if (lineNumber === doc.lines) {
    // A last line has no break after it, so the removal takes the break before the first line.
    return { from: Math.max(0, first.from - 1), to: line.to, insert: "" };
  }
  return { from: first.from, to: line.to + 1, insert: "" };
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

/** The annotations history and the user-event listeners read, from `transaction`. */
function carriedAnnotations(transaction: Transaction): Annotation<unknown>[] {
  const carried: Annotation<unknown>[] = [];
  const userEvent = transaction.annotation(Transaction.userEvent);
  if (userEvent !== undefined) carried.push(Transaction.userEvent.of(userEvent));
  const addToHistory = transaction.annotation(Transaction.addToHistory);
  if (addToHistory !== undefined) carried.push(Transaction.addToHistory.of(addToHistory));
  const isolation = transaction.annotation(isolateHistory);
  if (isolation !== undefined) carried.push(isolateHistory.of(isolation));
  return carried;
}

function removalSpec(transaction: Transaction, edit: Edit): TransactionSpec {
  return {
    changes: edit,
    selection: EditorSelection.cursor(edit.from + edit.insert.length),
    annotations: carriedAnnotations(transaction),
    scrollIntoView: transaction.scrollIntoView,
  };
}

/**
 * The one edit of `transaction`, when it takes a whole quote: the quote's own text with its line,
 * or the break beside it when the line on the other side has text. A blank line beside a quote
 * is just a line, so deleting it leaves the quote alone.
 */
function quoteRemovalOf(transaction: Transaction): TransactionSpec | null {
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
    return removalSpec(transaction, lineRemoval(doc, line.number));
  }
  return null;
}

/**
 * The change filter. A change may cover a whole token or stay clear of it, never reach into it;
 * CodeMirror filters a transaction's changes whole, so one such change drops the transaction's.
 */
export function keepsReplyQuotesWhole(transaction: Transaction): boolean {
  if (
    !transaction.docChanged ||
    transaction.isUserEvent("undo") ||
    transaction.isUserEvent("redo")
  ) {
    return true;
  }
  const spans = tokenSpans(transaction.startState.doc);
  let whole = true;
  transaction.changes.iterChangedRanges((fromA, toA) => {
    for (const span of spans) {
      const overlaps = fromA < span.to && toA > span.from;
      const covers = fromA <= span.from && toA >= span.to;
      if (overlaps && !covers) whole = false;
    }
  });
  return whole;
}

/**
 * The transaction filter. A deletion of a whole quote removes it, joining the text around it.
 * Otherwise inserted text loses its marks, and each token that has text on its line is set on
 * a line of its own. Those edits follow the original transaction as sequential specs, so its
 * selection, effects, annotations and history grouping carry over.
 */
export function normalizeReplyQuoteTransaction(
  transaction: Transaction,
): TransactionSpec | readonly TransactionSpec[] | Transaction {
  if (
    !transaction.docChanged ||
    transaction.isUserEvent("undo") ||
    transaction.isUserEvent("redo")
  ) {
    return transaction;
  }
  const removal = quoteRemovalOf(transaction);
  if (removal) return removal;

  const strips: Edit[] = [];
  transaction.changes.iterChanges((_fromA, _toA, fromB, _toB, inserted) => {
    strips.push(...markRanges(inserted.toString(), fromB));
  });
  const stripChanges = ChangeSet.of(strips, transaction.newDoc.length);
  const afterStrip = stripChanges.apply(transaction.newDoc);

  const breaks = new Set<number>();
  for (const span of tokenSpans(afterStrip)) {
    const line = afterStrip.lineAt(span.from);
    if (span.from > line.from) breaks.add(span.from);
    if (span.to < line.to) breaks.add(span.to);
  }
  const breakEdits: ChangeSpec = [...breaks]
    .sort((a, b) => a - b)
    .map((position) => ({ from: position, to: position, insert: "\n" }));

  if (strips.length === 0 && breaks.size === 0) return transaction;
  const specs: TransactionSpec[] = [
    {
      changes: transaction.changes,
      selection: transaction.selection,
      effects: transaction.effects,
      annotations: carriedAnnotations(transaction),
      scrollIntoView: transaction.scrollIntoView,
    },
  ];
  if (strips.length > 0) {
    specs.push({ changes: strips, sequential: true });
  }
  if (breaks.size > 0) {
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
  update: (decorations, transaction) =>
    transaction.docChanged || transaction.startState.readOnly !== transaction.state.readOnly
      ? quoteDecorations(transaction.state)
      : decorations,
  provide: (field) => [
    EditorView.decorations.from(field),
    EditorView.atomicRanges.of((view) => view.state.field(field)),
  ],
});

/** Quote cards and their atomic caret, plus the clipboard's `> ` markdown for copy, cut and drag. */
export const replyQuoteExtensions: Extension = [
  replyQuoteField,
  EditorView.clipboardOutputFilter.of((text) => replyQuoteClipboardText(text)),
];
