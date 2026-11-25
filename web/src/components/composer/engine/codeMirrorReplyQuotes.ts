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
import { killBarriers, killed } from "./codeMirrorReadline";

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

/**
 * Marks an edit the app makes on the user's behalf, such as an insert at the caret. It may change
 * the text around a card, but never removes or alters a card.
 */
export const programmaticEdit = Annotation.define<true>();

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
  // A controlled sync brings in the owner's document as it is, cards and all.
  if (!transaction.docChanged || isHistory(transaction) || transaction.annotation(controlledUpdate))
    return true;
  const cards = transaction.startState.field(cardField).cards;
  if (cards.length === 0) return true;
  const programmatic = transaction.annotation(programmaticEdit) === true;
  const partial = new Set<Card>();
  transaction.changes.iterChangedRanges((fromA, toA) => {
    for (const card of cards) {
      const reaches = fromA < card.to && toA > card.from;
      // Only a user edit may cover a whole card, which is how its removal happens.
      if (reaches && (programmatic || !(fromA <= card.from && toA >= card.to))) partial.add(card);
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
 * The transaction filter. Strip tokens from inserted text and keep surviving cards on separate
 * lines. Backward/forward deletion of a card-adjacent newline removes that card. The user's
 * transaction comes first, and these fixes follow it as one sequential spec.
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

  // Backspace after a card or Delete before one removes it; any other join keeps cards on their own lines.
  const startCards = transaction.startState.field(cardField).cards;
  const deleted: { from: number; to: number }[] = [];
  transaction.changes.iterChanges((fromA, toA) => deleted.push({ from: fromA, to: toA }));
  const [only] = deleted;
  let removal: { from: number; to: number } | null = null;
  if (
    inserts.length === 0 &&
    deleted.length === 1 &&
    only !== undefined &&
    only.to - only.from === 1 &&
    transaction.startState.sliceDoc(only.from, only.to) === "\n"
  ) {
    const above = startCards.find((card) => card.to === only.from);
    const below = startCards.find((card) => card.from === only.to);
    const target = transaction.isUserEvent("delete.backward")
      ? above
      : transaction.isUserEvent("delete.forward")
        ? below
        : undefined;
    if (target) {
      // The card goes with the blank line that separates it, as its X removes it. The span is
      // mapped into the new document, where the break is already gone.
      const span = cardRemoval(
        transaction.startState.doc,
        transaction.startState.doc.lineAt(target.from).number,
      );
      if (span) {
        // The span keeps the break the key removed. When it doesn't (Delete at a card with no
        // blank line above), the break before the card goes instead, so the break after it stays.
        const whole = span.from <= only.from && only.to <= span.to;
        const range = whole ? span : { from: only.from, to: target.to };
        removal = {
          from: transaction.changes.mapPos(range.from, 1),
          to: transaction.changes.mapPos(range.to, -1),
        };
      }
    }
  }
  const breaks = new Set<number>();
  if (removal === null) {
    for (let number = 1; number <= doc.lines; number += 1) {
      const line = doc.line(number);
      if (!line.text.includes(REPLY_QUOTE_MARK)) continue;
      for (const token of replyQuoteTokenRanges(line.text)) {
        if (token.start > 0) breaks.add(line.from + token.start);
        if (token.end < line.text.length) breaks.add(line.from + token.end);
      }
    }
  }
  apply(
    removal
      ? [removal]
      : [...breaks].sort((a, b) => a - b).map((position) => ({ from: position, insert: "\n" })),
  );

  // The seed is the first line: text above it moves to the start of the text after it. Only
  // while the seed itself stays first; once it is removed, the next card must not take its place.
  let selection: EditorSelection | undefined;
  const seedId = parseReplyQuoteLine(transaction.startState.doc.line(1).text)?.id;
  const first = transaction.startState.facet(pinnedFirstCard) ? firstCardRange(doc) : null;
  const seedKept =
    first !== null &&
    seedId !== undefined &&
    parseReplyQuoteLine(doc.lineAt(first.from).text)?.id === seedId;
  if (first && seedKept && first.from > 0) {
    const prefix = doc.sliceString(0, first.from - 1);
    const cardLine = doc.lineAt(first.to);
    const next = cardLine.number < doc.lines ? doc.line(cardLine.number + 1) : null;
    const joinsTail = next !== null && parseReplyQuoteLine(next.text) === null;
    const at = next ? next.from : first.to;
    const insert = joinsTail ? prefix : next ? `${prefix}\n` : `\n${prefix}`;
    apply([
      { from: 0, to: first.from },
      { from: at, insert },
    ]);
    const insertedAt = at - first.from + (next ? 0 : 1);
    selection = EditorSelection.single(insertedAt + prefix.length);
  }

  // A card is never the last line, so the text after it always has a line to go to.
  if (parseReplyQuoteLine(doc.line(doc.lines).text) !== null) {
    apply([{ from: doc.length, insert: "\n" }]);
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

/** Each card and the breaks beside it, which a kill can't reach, so no kill removes a card. */
function cardBarriers(doc: Text): { from: number; to: number }[] {
  return findCards(doc).map((card) => ({
    from: Math.max(0, card.from - 1),
    to: Math.min(doc.length, card.to + 1),
  }));
}

/** The card extensions: each card is atomic and paints as a card, and copies as `> ` markdown. */
export const replyQuoteExtensions: Extension = [
  cardField,
  cardArrowKeys(stepOverCard),
  EditorState.changeFilter.of(keepCardsWhole),
  EditorState.transactionFilter.of(normalizeCards),
  EditorView.clipboardOutputFilter.of((text) => replyQuoteClipboardText(text)),
  killBarriers.of(cardBarriers),
];
