// Reply quotes inside the CodeMirror composer. A quote line paints as a card that the caret
// steps over, that a deletion removes whole, and whose X removes it. Lazily loaded with the
// editor; it imports only CodeMirror and the pure document mapping.

import {
  Annotation,
  EditorSelection,
  StateField,
  type EditorState,
  type Extension,
  type Text,
  type Transaction,
  type TransactionSpec,
} from "@codemirror/state";
import { Decoration, EditorView, WidgetType, type DecorationSet } from "@codemirror/view";
import { removeReplyQuote as removeDraftQuote } from "@/lib/replyDraft";
import {
  parseReplyQuoteLine,
  replyQuoteClipboardText,
  REPLY_QUOTE_MARK,
  unweaveReplyDraft,
  weaveReplyDraft,
  withoutReplyQuoteMarks,
} from "@/lib/replyDraftDocument";

/** The quote text the card shows before it truncates; the full text stays in its title. */
const QUOTE_PREVIEW_LENGTH = 120;
const CARD_CLASS =
  "min-w-0 flex-1 bg-muted/40 rounded-md border-l-2 border-l-primary/60 px-2 py-1.5 text-sm text-muted-foreground";
const REMOVE_CLASS =
  "mt-0.5 shrink-0 rounded-full text-muted-foreground hover:text-foreground disabled:opacity-60";
const X_ICON =
  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-3.5" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

interface QuoteWidgetSource {
  id: string;
  text: string;
}

/** A quote's card, painted over its line. */
class ReplyQuoteWidget extends WidgetType {
  readonly quote: QuoteWidgetSource;

  constructor(quote: QuoteWidgetSource) {
    super();
    this.quote = quote;
  }

  override eq(other: ReplyQuoteWidget): boolean {
    return other.quote.id === this.quote.id && other.quote.text === this.quote.text;
  }

  override toDOM(view: EditorView): HTMLElement {
    const { id, text } = this.quote;
    const row = document.createElement("div");
    row.className = "flex items-start gap-2 py-1.5";
    row.setAttribute("data-testid", "composer-reply-quote");
    const card = document.createElement("blockquote");
    card.className = CARD_CLASS;
    card.title = text;
    const preview = document.createElement("span");
    preview.className = "block truncate";
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
    remove.innerHTML = X_ICON;
    // Keeps the editor's focus and caret where they are.
    remove.addEventListener("mousedown", (event) => event.preventDefault());
    remove.addEventListener("click", () => removeQuoteFromView(view, id));
    row.append(card, remove);
    return row;
  }
}

/** A quote line and the break after it, so removing the quote leaves the lines around it. */
function quoteLineRange(doc: Text, lineNumber: number): { from: number; to: number } {
  const line = doc.line(lineNumber);
  if (lineNumber < doc.lines) return { from: line.from, to: line.to + 1 };
  // A quote on the last line has no break after it, so it takes the break before it.
  return { from: Math.max(0, line.from - 1), to: line.to };
}

function quoteDecorations(state: EditorState): DecorationSet {
  const ranges = [];
  for (let lineNumber = 1; lineNumber <= state.doc.lines; lineNumber += 1) {
    const quote = parseReplyQuoteLine(state.doc.line(lineNumber).text);
    if (!quote) continue;
    const { from, to } = quoteLineRange(state.doc, lineNumber);
    ranges.push(
      Decoration.replace({ block: true, widget: new ReplyQuoteWidget(quote) }).range(from, to),
    );
  }
  return Decoration.set(ranges, true);
}

/** Paints each quote line as a card and makes it atomic, so the caret steps over it whole. */
const replyQuoteField = StateField.define<DecorationSet>({
  create: quoteDecorations,
  update: (decorations, transaction) =>
    transaction.docChanged ? quoteDecorations(transaction.state) : decorations,
  provide: (field) => [
    EditorView.decorations.from(field),
    EditorView.atomicRanges.of((view) => view.state.field(field)),
  ],
});

/** Marks the edits that remove a quote whole, which the quote-line rules leave alone. */
const quoteRemoval = Annotation.define<true>();

/**
 * The change that removes the quote on `lineNumber`, as the textarea's removal does: the text
 * around it joins as paragraphs. A minimal change, so undo brings the quote back exactly.
 */
function quoteRemovalChange(
  doc: Text,
  lineNumber: number,
): { from: number; to: number; insert: string } {
  const current = doc.toString();
  const id = parseReplyQuoteLine(doc.line(lineNumber).text)!.id;
  const next = weaveReplyDraft(removeDraftQuote(unweaveReplyDraft(current), id));
  let prefix = 0;
  const limit = Math.min(current.length, next.length);
  while (prefix < limit && current[prefix] === next[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < limit - prefix &&
    current[current.length - 1 - suffix] === next[next.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  return {
    from: prefix,
    to: current.length - suffix,
    insert: next.slice(prefix, next.length - suffix),
  };
}

/** Removes the quote with `id`, as one undoable edit; a no-op when the document lacks it. */
function removeQuoteFromView(view: EditorView, id: string): boolean {
  if (view.state.readOnly) return false;
  const doc = view.state.doc;
  for (let lineNumber = 1; lineNumber <= doc.lines; lineNumber += 1) {
    if (parseReplyQuoteLine(doc.line(lineNumber).text)?.id !== id) continue;
    const change = quoteRemovalChange(doc, lineNumber);
    view.dispatch({
      changes: change,
      selection: EditorSelection.cursor(change.from + change.insert.length),
      userEvent: "delete",
      annotations: quoteRemoval.of(true),
    });
    return true;
  }
  return false;
}

/**
 * Keeps each quote line whole through any deletion, whichever key or command made it. A quote
 * always has a line break before it and keeps its own line, so the document reads back to the
 * same draft. Deleting the break before a quote removes the quote, as Delete at the end of the
 * line above it does.
 */
export function keepReplyQuoteLinesWhole(transaction: Transaction): TransactionSpec | Transaction {
  if (!transaction.docChanged || transaction.annotation(quoteRemoval)) return transaction;
  const edits: { fromA: number; toA: number; insert: string }[] = [];
  transaction.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
    edits.push({ fromA, toA, insert: inserted.toString() });
  });
  // Several changes can overlap once one is widened, so only a single edit is rewritten.
  if (edits.length !== 1) return transaction;
  const { fromA, toA, insert } = edits[0]!;
  const doc = transaction.startState.doc;
  if (fromA >= toA) return transaction;
  // A whole quote line, with its break, is a removal of that quote.
  const removed = doc.lineAt(fromA);
  if (
    removed.from === fromA &&
    removed.number < doc.lines &&
    toA === removed.to + 1 &&
    parseReplyQuoteLine(removed.text) !== null
  ) {
    return removal(removed.number);
  }
  const quoteLine = doc.lineAt(toA);
  if (
    quoteLine.from !== toA ||
    quoteLine.number === doc.lines ||
    parseReplyQuoteLine(quoteLine.text) === null
  ) {
    return transaction;
  }
  if (fromA === toA - 1) {
    // Only the break before the quote: the quote goes with it, and the line above stays.
    if (insert === "") return removal(quoteLine.number);
    const quote = quoteLineRange(doc, quoteLine.number);
    return replacement(fromA, quote.to, `${insert}\n`, fromA + insert.length);
  }
  // Deleting whole lines above the quote leaves it after an intact break. Any other deletion
  // keeps the break before the quote, so the text left above can't join the quote's line.
  const above = doc.lineAt(fromA);
  if (fromA === above.from && above.number > 1) return transaction;
  return replacement(fromA, toA - 1, insert, fromA + insert.length);

  function removal(lineNumber: number): TransactionSpec {
    const change = quoteRemovalChange(doc, lineNumber);
    return {
      changes: change,
      selection: EditorSelection.cursor(change.from + change.insert.length),
      userEvent: "delete",
      annotations: quoteRemoval.of(true),
    };
  }

  function replacement(from: number, to: number, text: string, caret: number): TransactionSpec {
    return {
      changes: { from, to, insert: text },
      selection: EditorSelection.cursor(caret),
      userEvent: transaction.isUserEvent("delete.cut") ? "delete.cut" : "delete",
    };
  }
}

/** A copy or cut of quote lines gives their `> ` markdown, never the marked line. */
const quoteClipboard = EditorView.domEventHandlers({
  copy(event, view) {
    return copyQuoteSelection(event, view, false);
  },
  cut(event, view) {
    return copyQuoteSelection(event, view, true);
  },
  paste(event, view) {
    const text = event.clipboardData?.getData("text/plain") ?? "";
    if (!text.includes(REPLY_QUOTE_MARK)) return false;
    event.preventDefault();
    if (!view.state.readOnly) {
      view.dispatch({
        ...view.state.replaceSelection(withoutReplyQuoteMarks(text)),
        userEvent: "input.paste",
      });
    }
    return true;
  },
});

function copyQuoteSelection(event: ClipboardEvent, view: EditorView, cut: boolean): boolean {
  const { from, to } = view.state.selection.main;
  const text = view.state.sliceDoc(from, to);
  if (from === to || !text.includes(REPLY_QUOTE_MARK) || !event.clipboardData) return false;
  event.clipboardData.setData("text/plain", replyQuoteClipboardText(text));
  event.preventDefault();
  if (cut && !view.state.readOnly) {
    view.dispatch({
      changes: { from, to },
      selection: EditorSelection.cursor(from),
      userEvent: "delete.cut",
    });
  }
  return true;
}

/** Quote cards, their atomic caret and deletion, and clipboard text, for one editor. */
export const replyQuoteExtensions: Extension = [replyQuoteField, quoteClipboard];
