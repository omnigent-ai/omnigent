// Reply quotes in the CodeMirror composer. Each card is a token line that paints as a card: the
// caret steps over it whole, and every edit leaves each token whole on a line of its own. Lazily
// loaded with the editor.

import { isolateHistory } from "@codemirror/commands";
import {
  Annotation,
  ChangeSet,
  EditorSelection,
  EditorState,
  Facet,
  Prec,
  StateField,
  Transaction,
  type ChangeSpec,
  type Extension,
  type Text,
  type TransactionSpec,
} from "@codemirror/state";
import { Decoration, EditorView, keymap, WidgetType, type DecorationSet } from "@codemirror/view";
import {
  parseReplyQuoteLine,
  quoteRemovalSpan,
  replyQuoteClipboardText,
  replyQuoteTokenRanges,
  REPLY_QUOTE_END,
  REPLY_QUOTE_MARK,
  withoutReplyQuoteMarks,
} from "@/lib/replyDraftDocument";
import { killed } from "./codeMirrorReadline";

/** The quote text a card shows before it truncates; the full text stays in its title. */
const QUOTE_PREVIEW_LENGTH = 120;
const ROW_CLASS = "flex w-full min-w-0 max-w-full items-start gap-2 py-1.5";
const CARD_CLASS =
  "min-w-0 max-w-full flex-1 overflow-hidden bg-muted/40 rounded-md border-l-2 border-l-primary/60 px-2 py-1.5 text-sm text-muted-foreground";
const REMOVE_CLASS =
  "mt-0.5 shrink-0 rounded-full text-muted-foreground hover:text-foreground disabled:opacity-60";
const X_ICON =
  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-3.5" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

/** Marks a transaction that syncs the controlled value in, which isn't a user edit. */
export const controlledUpdate = Annotation.define<true>();

/** Whether the first card is the side chat's seed, so text typed above it lands below it. */
export const pinnedFirstCard = Facet.define<boolean, boolean>({
  combine: (values) => values[values.length - 1] ?? false,
});

interface Card {
  from: number;
  to: number;
  id: string;
  text: string;
}

interface CardState {
  cards: Card[];
  readOnly: boolean;
  decorations: DecorationSet;
}

function findCards(doc: Text): Card[] {
  const cards: Card[] = [];
  for (let number = 1; number <= doc.lines; number += 1) {
    const line = doc.line(number);
    const quote = parseReplyQuoteLine(line.text);
    if (quote) cards.push({ from: line.from, to: line.to, ...quote });
  }
  return cards;
}

/** The card's range and the change that removes it with its line, or null when the line isn't one. */
function cardRemoval(doc: Text, lineNumber: number): { from: number; to: number } | null {
  if (parseReplyQuoteLine(doc.line(lineNumber).text) === null) return null;
  const { first, last } = quoteRemovalSpan(doc.toString().split("\n"), lineNumber - 1);
  if (last + 1 < doc.lines) return { from: doc.line(first + 1).from, to: doc.line(last + 2).from };
  if (first > 0) return { from: doc.line(first).to, to: doc.line(last + 1).to };
  return { from: 0, to: doc.length };
}

/** Removes the card on the line at `position`, as its X does. */
function removeCardAt(view: EditorView, position: number): void {
  if (view.state.readOnly) return;
  const removal = cardRemoval(view.state.doc, view.state.doc.lineAt(position).number);
  if (removal) view.dispatch({ changes: removal, userEvent: "delete" });
}

/** A card's own markup: the quote as a card, with an X that removes it. */
class ReplyQuoteWidget extends WidgetType {
  readonly card: Card;
  readonly readOnly: boolean;

  constructor(card: Card, readOnly: boolean) {
    super();
    this.card = card;
    this.readOnly = readOnly;
  }

  override eq(other: ReplyQuoteWidget): boolean {
    return (
      other.card.id === this.card.id &&
      other.card.text === this.card.text &&
      other.readOnly === this.readOnly
    );
  }

  override toDOM(view: EditorView): HTMLElement {
    const { text } = this.card;
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
    remove.addEventListener("click", () => removeCardAt(view, view.posAtDOM(row)));
    row.append(card, remove);
    return row;
  }
}

function cardState(state: EditorState): CardState {
  const cards = findCards(state.doc);
  const readOnly = state.readOnly;
  return {
    cards,
    readOnly,
    decorations: Decoration.set(
      // Only the card's own line is replaced, so the line after it stays reachable.
      cards.map((card) =>
        Decoration.replace({ block: true, widget: new ReplyQuoteWidget(card, readOnly) }).range(
          card.from,
          card.to,
        ),
      ),
    ),
  };
}

/** The cards, rebuilt after each edit and on a read-only change, which their X depends on. */
const cardField = StateField.define<CardState>({
  create: cardState,
  update: (value, transaction) =>
    transaction.docChanged || transaction.startState.readOnly !== transaction.state.readOnly
      ? cardState(transaction.state)
      : value,
  provide: (field) => [
    EditorView.decorations.from(field, (value) => value.decorations),
    EditorView.atomicRanges.of((view) => view.state.field(field).decorations),
  ],
});

function isHistory(transaction: Transaction): boolean {
  return transaction.isUserEvent("undo") || transaction.isUserEvent("redo");
}

/**
 * The change filter. A change that reaches into a card without covering it is suppressed inside
 * that card only, so the rest of the edit lands. A change covering a whole card removes it.
 */
function keepCardsWhole(transaction: Transaction): boolean | readonly number[] {
  if (!transaction.docChanged || isHistory(transaction)) return true;
  const cards = transaction.startState.field(cardField).cards;
  if (cards.length === 0) return true;
  const partial = new Set<Card>();
  transaction.changes.iterChangedRanges((fromA, toA) => {
    for (const card of cards) {
      const reaches = fromA < card.to && toA > card.from;
      if (reaches && !(fromA <= card.from && toA >= card.to)) partial.add(card);
    }
  });
  if (partial.size === 0) return true;
  return cards.filter((card) => partial.has(card)).flatMap((card) => [card.from, card.to]);
}

function hasMark(text: string): boolean {
  return text.includes(REPLY_QUOTE_MARK) || text.includes(REPLY_QUOTE_END);
}

/** The first card's range in `doc`, if there is one. */
function firstCardRange(doc: Text): { from: number; to: number } | null {
  for (let number = 1; number <= doc.lines; number += 1) {
    const line = doc.line(number);
    const [token] = replyQuoteTokenRanges(line.text);
    if (token) return { from: line.from + token.start, to: line.from + token.end };
  }
  return null;
}

/**
 * The transaction's own changes, with the annotations the editor keeps: undo grouping, the
 * readline kill, and history isolation. The rest of a transaction is carried by the caller.
 */
function userSpec(transaction: Transaction): TransactionSpec {
  const annotations: Annotation<unknown>[] = [];
  const addToHistory = transaction.annotation(Transaction.addToHistory);
  if (addToHistory !== undefined) annotations.push(Transaction.addToHistory.of(addToHistory));
  const isolation = transaction.annotation(isolateHistory);
  if (isolation !== undefined) annotations.push(isolateHistory.of(isolation));
  const killedText = transaction.annotation(killed);
  if (killedText !== undefined) annotations.push(killed.of(killedText));
  return {
    changes: transaction.changes,
    selection: transaction.selection,
    effects: transaction.effects,
    annotations,
    userEvent: transaction.annotation(Transaction.userEvent),
    scrollIntoView: transaction.scrollIntoView,
  };
}

/**
 * The transaction filter. Plain text never carries a token, so tokens in inserted text are
 * stripped. Text left beside a card goes onto its own line; a deletion that joins text onto a card
 * removes the card, as Backspace or Delete at its edge does. The user's transaction comes first,
 * and these fixes follow it as one sequential spec.
 */
function normalizeCards(
  transaction: Transaction,
): TransactionSpec | readonly TransactionSpec[] | Transaction {
  if (
    !transaction.docChanged ||
    isHistory(transaction) ||
    transaction.annotation(controlledUpdate)
  ) {
    return transaction;
  }
  const inserts: { from: number; to: number; text: string }[] = [];
  transaction.changes.iterChanges((_fromA, _toA, fromB, toB, inserted) => {
    const text = inserted.toString();
    if (text !== "") inserts.push({ from: fromB, to: toB, text });
  });
  const insertsMarks = inserts.some((insert) => hasMark(insert.text));
  if (!insertsMarks && transaction.startState.field(cardField).cards.length === 0) {
    return transaction;
  }

  let doc = transaction.newDoc;
  let fixes = ChangeSet.empty(doc.length);
  const apply = (changes: ChangeSpec[]) => {
    if (changes.length === 0) return;
    const next = ChangeSet.of(changes, doc.length);
    fixes = fixes.compose(next);
    doc = next.apply(doc);
  };

  apply(
    inserts
      .filter((insert) => hasMark(insert.text))
      .map((insert) => ({
        from: insert.from,
        to: insert.to,
        insert: withoutReplyQuoteMarks(insert.text),
      })),
  );

  // A deletion with nothing inserted removes the card it joins text onto; any other edit splits.
  const pureDeletion = inserts.length === 0;
  const removals: ChangeSpec[] = [];
  const breaks = new Set<number>();
  for (let number = 1; number <= doc.lines; number += 1) {
    const line = doc.line(number);
    if (!line.text.includes(REPLY_QUOTE_MARK)) continue;
    for (const token of replyQuoteTokenRanges(line.text)) {
      const from = line.from + token.start;
      const to = line.from + token.end;
      const beside = token.start > 0 || token.end < line.text.length;
      if (!beside) continue;
      if (pureDeletion) removals.push({ from, to });
      else {
        if (token.start > 0) breaks.add(from);
        if (token.end < line.text.length) breaks.add(to);
      }
    }
  }
  apply(
    pureDeletion
      ? removals
      : [...breaks].sort((a, b) => a - b).map((position) => ({ from: position, insert: "\n" })),
  );

  // The seed is the first line: text above it moves to the line after it.
  let selection: EditorSelection | undefined;
  const first = transaction.startState.facet(pinnedFirstCard) ? firstCardRange(doc) : null;
  if (first && first.from > 0) {
    const prefix = doc.sliceString(0, first.from - 1);
    apply([
      { from: 0, to: first.from },
      { from: first.to, insert: `\n${prefix}` },
    ]);
    selection = EditorSelection.single(first.to - first.from + 1 + prefix.length);
  }

  if (fixes.empty) return transaction;
  return [userSpec(transaction), { changes: fixes, selection, sequential: true }];
}

const cardArrowKeys = (step: (view: EditorView, direction: -1 | 1) => boolean) =>
  Prec.highest(
    keymap.of([
      { key: "ArrowLeft", run: (view) => step(view, -1) },
      { key: "ArrowRight", run: (view) => step(view, 1) },
    ]),
  );

/**
 * Left or right past a card, to the text beside it: from the start of the line after a card to the
 * end of the line above, and from the end of a line to the start of the line after a card. Other
 * steps keep the default, so ArrowUp and ArrowDown are untouched.
 */
function stepOverCard(view: EditorView, direction: -1 | 1): boolean {
  const range = view.state.selection.main;
  if (!range.empty) return false;
  const doc = view.state.doc;
  const line = doc.lineAt(range.head);
  const isCard = (number: number) => parseReplyQuoteLine(doc.line(number).text) !== null;
  let target: number;
  if (direction < 0) {
    if (range.head !== line.from || line.number === 1 || !isCard(line.number - 1)) return false;
    let number = line.number - 1;
    while (number >= 1 && isCard(number)) number -= 1;
    if (number < 1) return false;
    target = doc.line(number).to;
  } else {
    if (range.head !== line.to || line.number === doc.lines || !isCard(line.number + 1)) {
      return false;
    }
    let number = line.number + 1;
    while (number <= doc.lines && isCard(number)) number += 1;
    if (number > doc.lines) return false;
    target = doc.line(number).from;
  }
  view.dispatch({ selection: { anchor: target }, scrollIntoView: true });
  return true;
}

/** The card extensions: each card is atomic and paints as a card, and copies as `> ` markdown. */
export const replyQuoteExtensions: Extension = [
  cardField,
  cardArrowKeys(stepOverCard),
  EditorState.changeFilter.of(keepCardsWhole),
  EditorState.transactionFilter.of(normalizeCards),
  EditorView.clipboardOutputFilter.of((text) => replyQuoteClipboardText(text)),
];
