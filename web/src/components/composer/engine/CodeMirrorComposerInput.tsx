// Plain-text CodeMirror adapter with injected composer services.

import { history, historyKeymap, isolateHistory, standardKeymap } from "@codemirror/commands";
import {
  Annotation,
  ChangeSet,
  Compartment,
  EditorSelection,
  EditorState,
  Facet,
  Prec,
  StateField,
  Transaction,
  type Extension,
  type Text,
  type TransactionSpec,
} from "@codemirror/state";
import {
  Decoration,
  EditorView,
  keymap,
  placeholder as placeholderText,
  WidgetType,
  type DecorationSet,
  type KeyBinding,
} from "@codemirror/view";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Ref,
  type RefObject,
} from "react";

import type { ComposerInsert, ComposerInsertAdjustment } from "@/lib/composerMentions";
import {
  hasQuoteMark,
  parseQuoteLine,
  quoteRemovalRange,
  quotesAsMarkdown,
  removeQuoteFromDocument,
  settleQuoteLines,
  stripQuoteMarks,
} from "@/lib/replyDraftDocument";
import { cn } from "@/lib/utils";

import type {
  ComposerEditorSurface,
  ComposerInputEvent,
  ComposerInputHeightMode,
  ComposerInputProps,
  ComposerKeyboardEvent,
  ComposerTextTarget,
  ComposerTokenAt,
} from "./composerInput";
import { killStops, readlineKeys } from "./codeMirrorReadline";

/** What the editor takes from the app, handed in by the loader. */
export interface CodeMirrorComposerEnvironment {
  /** Whether the platform is macOS, which binds the readline keys. */
  mac: boolean;
  /** The attribute that marks a composer input's focusable node. */
  inputAttribute: string;
  /** The attribute that carries an input's id within its composer. */
  inputIdAttribute: string;
  /** Make the surface findable from its node; returns the undo. */
  registerSurface: (surface: ComposerEditorSurface) => () => void;
  /** Where each tagged @-mention token sits in the text, in text order. */
  findMentionTokens: (text: string, tokens: readonly string[]) => readonly TokenSpan[];
  /** Adds the space an insert needs so the painted tokens it touches stay whole. */
  separateMentionEdges: (
    value: string,
    insert: ComposerInsert,
    tokens: readonly string[],
  ) => ComposerInsertAdjustment | null;
  /** The class that paints a mention token, shared with the textarea's mirror. */
  mentionTokenClass: string;
  /** The class that paints the leading command, shared with the textarea's mirror. */
  commandTokenClass: string;
}

/** A token's [start, end) range in the text. */
export interface TokenSpan {
  start: number;
  end: number;
}

/** The draft's height cap before it scrolls, matching the textarea's. */
const COMPOSER_MAX_HEIGHT_PX = 180;

/** Marks a transaction that syncs the controlled value in, which isn't a user edit. */
const controlledUpdate = Annotation.define<true>();

/** The undo or redo that made these transactions' edit, if either did. */
function historyDirectionOfTransactions(
  transactions: readonly Transaction[],
): "undo" | "redo" | undefined {
  if (transactions.some((transaction) => transaction.isUserEvent("undo"))) return "undo";
  if (transactions.some((transaction) => transaction.isUserEvent("redo"))) return "redo";
  return undefined;
}

/**
 * Rewrites a user input (typing, paste, completion, dictation) in the same transaction, so undo
 * removes the insert and any space it added together. IME composition is skipped. Only the
 * tokens this editor paints are kept whole.
 */
function adjustedInsertTransaction(
  transaction: Transaction,
  separateMentionEdges: CodeMirrorComposerEnvironment["separateMentionEdges"],
): TransactionSpec | Transaction {
  if (
    !transaction.docChanged ||
    transaction.annotation(controlledUpdate) ||
    !transaction.isUserEvent("input") ||
    transaction.isUserEvent("input.type.compose")
  ) {
    return transaction;
  }
  const paintedTokens = transaction.startState.facet(mentionTokensFacet);
  if (paintedTokens.length === 0) return transaction;
  const inserts: { start: number; end: number; text: string }[] = [];
  transaction.changes.iterChanges((start, end, _fromB, _toB, inserted) => {
    inserts.push({ start, end, text: inserted.toString() });
  });
  const insert = inserts.length === 1 ? inserts[0] : undefined;
  if (!insert) return transaction;
  const adjusted = separateMentionEdges(
    transaction.startState.doc.toString(),
    insert,
    paintedTokens,
  );
  if (!adjusted) return transaction;
  const isolation = transaction.annotation(isolateHistory);
  const addToHistory = transaction.annotation(Transaction.addToHistory);
  return {
    changes: { from: insert.start, to: insert.end, insert: adjusted.text },
    selection: EditorSelection.cursor(insert.start + adjusted.caret),
    userEvent: transaction.annotation(Transaction.userEvent),
    annotations: [
      ...(isolation === undefined ? [] : [isolateHistory.of(isolation)]),
      ...(addToHistory === undefined ? [] : [Transaction.addToHistory.of(addToHistory)]),
    ],
    effects: transaction.effects,
    scrollIntoView: transaction.scrollIntoView,
  };
}

/** The mention tokens the editor paints; reconfigured through a compartment when they change. */
const mentionTokensFacet = Facet.define<readonly string[], readonly string[]>({
  combine: (inputs) => inputs[inputs.length - 1] ?? [],
});

/**
 * Paints the spans `findSpans` finds in the editor's text, rebuilt when the text or the facet's
 * value changes. Atomic spans make the caret, selection and deletion treat each as one character.
 */
function tokenDecorationField<Value>(
  facet: Facet<Value, Value>,
  mark: Decoration,
  findSpans: (text: string, value: Value) => readonly TokenSpan[],
  atomic: boolean,
): Extension {
  const build = (state: EditorState): DecorationSet =>
    Decoration.set(
      findSpans(state.doc.toString(), state.facet(facet)).map((span) =>
        mark.range(span.start, span.end),
      ),
    );
  return StateField.define<DecorationSet>({
    create: build,
    update(decorations, transaction) {
      const valueChanged = transaction.startState.facet(facet) !== transaction.state.facet(facet);
      return transaction.docChanged || valueChanged ? build(transaction.state) : decorations;
    },
    provide: (decorated) => [
      EditorView.decorations.from(decorated),
      ...(atomic ? [EditorView.atomicRanges.of((view) => view.state.field(decorated))] : []),
    ],
  });
}

/** Paints each tagged mention token, atomic so a token acts as one character. */
function mentionTokenField(environment: CodeMirrorComposerEnvironment): Extension {
  const mark = Decoration.mark({
    class: environment.mentionTokenClass,
    attributes: { "data-testid": "composer-mention-token" },
  });
  return tokenDecorationField(
    mentionTokensFacet,
    mark,
    (text, tokens) => environment.findMentionTokens(text, tokens),
    true,
  );
}

type CommandToken = string | ComposerTokenAt | null;

/** The command token the editor paints; reconfigured through a compartment when it changes. */
const commandTokenFacet = Facet.define<CommandToken, CommandToken>({
  combine: (inputs) => inputs[inputs.length - 1] ?? null,
});

/**
 * Where `token` leads `text` after any whitespace, else null; a placed token matches only at
 * its offset.
 */
export function leadingCommandSpan(text: string, token: CommandToken): TokenSpan | null {
  if (!token) return null;
  if (typeof token !== "string") {
    const end = token.start + token.token.length;
    return token.token && text.slice(token.start, end) === token.token
      ? { start: token.start, end }
      : null;
  }
  const start = text.length - text.trimStart().length;
  const end = start + token.length;
  if (text.slice(start, end) !== token) return null;
  // A longer word that merely starts with the token, like /contexts, isn't the command.
  if (end < text.length && !/\s/.test(text[end] ?? "")) return null;
  return { start, end };
}

/** `props.commandToken` with a placed token's offset moved from the value into the document. */
function documentCommandToken(props: ComposerInputProps): CommandToken {
  const token = props.commandToken ?? null;
  if (token === null || typeof token === "string") return token;
  const start = normalizeLineBreaks((props.value ?? "").slice(0, token.start)).length;
  return { token: token.token, start };
}

/** Paints the draft's leading command token, checked against the editor's own text. */
function commandTokenField(environment: CodeMirrorComposerEnvironment): Extension {
  const mark = Decoration.mark({
    class: environment.commandTokenClass,
    attributes: { "data-testid": "composer-command-token" },
  });
  return tokenDecorationField(
    commandTokenFacet,
    mark,
    (text, token) => {
      const span = leadingCommandSpan(text, token);
      return span ? [span] : [];
    },
    false,
  );
}

/** How much of a quote a card shows; the full quote is its title and is read to screen readers. */
const QUOTE_PREVIEW_LENGTH = 120;
const REMOVE_QUOTE_ICON =
  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-3.5" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

/** A reply quote's card, with an X that removes it. */
class QuoteCardWidget extends WidgetType {
  readonly id: string;
  readonly text: string;
  readonly readOnly: boolean;

  constructor(id: string, text: string, readOnly: boolean) {
    super();
    this.id = id;
    this.text = text;
    this.readOnly = readOnly;
  }

  override eq(other: QuoteCardWidget): boolean {
    return other.id === this.id && other.text === this.text && other.readOnly === this.readOnly;
  }

  override toDOM(view: EditorView): HTMLElement {
    const row = document.createElement("div");
    row.className = "flex w-full min-w-0 max-w-full select-none items-start gap-2 py-1.5";
    row.setAttribute("data-testid", "composer-reply-quote");
    const card = document.createElement("blockquote");
    card.className =
      "min-w-0 max-w-full flex-1 overflow-hidden bg-muted/40 rounded-md border-l-2 border-l-primary/60 px-2 py-1.5 text-sm text-muted-foreground";
    card.title = this.text;
    const preview = document.createElement("span");
    preview.className = "block min-w-0 truncate";
    preview.setAttribute("aria-hidden", "true");
    preview.textContent =
      this.text.length > QUOTE_PREVIEW_LENGTH
        ? `${this.text.slice(0, QUOTE_PREVIEW_LENGTH)}…`
        : this.text;
    const full = document.createElement("span");
    full.className = "sr-only";
    full.textContent = this.text;
    card.append(preview, full);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className =
      "mt-0.5 shrink-0 rounded-full text-muted-foreground hover:text-foreground disabled:opacity-60";
    remove.setAttribute("aria-label", "Remove quote");
    remove.disabled = this.readOnly;
    remove.innerHTML = REMOVE_QUOTE_ICON;
    // Keeps the editor's focus and caret where they are.
    remove.addEventListener("mousedown", (event) => event.preventDefault());
    const { id } = this;
    remove.addEventListener("click", () => {
      const range = quoteRemovalRange(view.state.doc.toString(), id);
      if (!range || view.state.readOnly) return;
      view.dispatch({ changes: range, userEvent: "delete" });
      // Pressed from the keyboard, the button held focus, and the removal takes it away.
      view.focus();
    });
    // A click on the card puts the caret on the line after it, never in the card's own text.
    row.addEventListener("mousedown", (event) => {
      event.preventDefault();
      if (remove.contains(event.target as Node)) return;
      const line = view.state.doc.lineAt(view.posAtDOM(row));
      view.dispatch({ selection: { anchor: Math.min(line.to + 1, view.state.doc.length) } });
      view.focus();
    });
    row.append(card, remove);
    return row;
  }
}

function quoteCards(state: EditorState): DecorationSet {
  const cards = [];
  for (let number = 1; number <= state.doc.lines; number += 1) {
    const line = state.doc.line(number);
    const quote = parseQuoteLine(line.text);
    if (!quote) continue;
    const widget = new QuoteCardWidget(quote.id, quote.text, state.readOnly);
    // Only the token's own line is replaced, so the line after the last card stays reachable.
    cards.push(Decoration.replace({ block: true, widget }).range(line.from, line.to));
  }
  return Decoration.set(cards);
}

/** Whether `transaction` inserts a mark or edits a line holding one, so a card may change. */
function touchesQuotes(transaction: Transaction): boolean {
  const { doc } = transaction.startState;
  let touches = false;
  transaction.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
    touches ||= hasQuoteMark(inserted.toString());
    const last = doc.lineAt(toA).number;
    for (let number = doc.lineAt(fromA).number; !touches && number <= last; number += 1) {
      touches = hasQuoteMark(doc.line(number).text);
    }
  });
  return touches;
}

/** Each quote's atomic card, rebuilt when an edit reaches one and when read-only changes. */
const quoteCardField = StateField.define<DecorationSet>({
  create: quoteCards,
  update: (cards, transaction) => {
    if (transaction.startState.readOnly !== transaction.state.readOnly) {
      return quoteCards(transaction.state);
    }
    if (!transaction.docChanged) return cards;
    return touchesQuotes(transaction)
      ? quoteCards(transaction.state)
      : cards.map(transaction.changes);
  },
  provide: (field) => [
    EditorView.decorations.from(field),
    EditorView.atomicRanges.of((view) => view.state.field(field)),
  ],
});

/** Whether the owner or the history made `transaction`, whose document is taken as it is. */
function isSyncOrHistory(transaction: Transaction): boolean {
  return (
    transaction.annotation(controlledUpdate) === true ||
    historyDirectionOfTransactions([transaction]) !== undefined
  );
}

/** Suppresses an edit inside each token it reaches without covering; covering one deletes it. */
function keepQuoteTokensWhole(transaction: Transaction): boolean | readonly number[] {
  if (isSyncOrHistory(transaction)) return true;
  const changed: [number, number][] = [];
  transaction.changes.iterChangedRanges((from, to) => changed.push([from, to]));
  const suppressed: number[] = [];
  const { doc } = transaction.startState;
  transaction.startState.field(quoteCardField).between(0, doc.length, (from, to) => {
    if (changed.some(([start, end]) => start < to && end > from && (start > from || end < to))) {
      suppressed.push(from, to);
    }
  });
  return suppressed.length > 0 ? suppressed : true;
}

/** The quote a caret's Backspace after its card, or Delete before it, removes: a lone break. */
function removedQuote(transaction: Transaction): string | null {
  const caret = transaction.startState.selection.main;
  const { doc } = transaction.startState;
  const changes: { from: number; to: number; inserted: number }[] = [];
  transaction.changes.iterChanges((from, to, _fromB, _toB, inserted) =>
    changes.push({ from, to, inserted: inserted.length }),
  );
  const [only] = changes;
  if (!caret.empty || changes.length !== 1 || !only || only.inserted > 0) return null;
  if (only.to - only.from !== 1 || doc.sliceString(only.from, only.to) !== "\n") return null;
  let card: number | null = null;
  if (only.to === caret.head) card = only.from;
  else if (only.from === caret.head) card = only.to;
  return card === null ? null : (parseQuoteLine(doc.lineAt(card).text)?.id ?? null);
}

/** `selection` with a caret that lands on a card moved past it, the way the caret was going. */
function caretOffCards(doc: Text, selection: EditorSelection, previous: number): EditorSelection {
  const { head, empty } = selection.main;
  if (!empty || parseQuoteLine(doc.lineAt(head).text) === null) return selection;
  const textLineEdge = (direction: 1 | -1): number | null => {
    const first = doc.lineAt(head).number + direction;
    for (let number = first; number >= 1 && number <= doc.lines; number += direction) {
      const line = doc.line(number);
      if (parseQuoteLine(line.text) === null) return direction > 0 ? line.from : line.to;
    }
    return null;
  };
  const backward = head < previous ? textLineEdge(-1) : null;
  const target = backward ?? textLineEdge(1) ?? textLineEdge(-1);
  return target === null ? selection : EditorSelection.single(target);
}

/**
 * Keeps every token whole on its own line, whatever made the edit, with one fix after the user's
 * transaction: see `removedQuote`, `settleQuoteLines` and `caretOffCards`.
 */
function settleQuoteCards(transaction: Transaction): Transaction | readonly TransactionSpec[] {
  if (isSyncOrHistory(transaction)) return transaction;
  // An edit that reaches no card, like an arrow or typing in text, needs only the caret kept off one.
  if (!touchesQuotes(transaction)) {
    const { newDoc, newSelection, startState } = transaction;
    const selection = caretOffCards(newDoc, newSelection, startState.selection.main.head);
    return selection.eq(newSelection)
      ? transaction
      : [transaction, { selection, sequential: true }];
  }
  const inserts: { from: number; to: number; text: string }[] = [];
  transaction.changes.iterChanges((_fromA, _toA, from, to, text) =>
    inserts.push({ from, to, text: text.toString() }),
  );
  const start = transaction.startState.doc.toString();
  const after = transaction.newDoc.toString();
  const removed = removedQuote(transaction);
  let settled = after;
  if (removed !== null) settled = removeQuoteFromDocument(start, removed);
  else {
    // Inserted text loses its tokens; the last insert goes first, so earlier offsets hold.
    for (const insert of inserts.reverse()) {
      const text = stripQuoteMarks(insert.text);
      settled = settled.slice(0, insert.from) + text + settled.slice(insert.to);
    }
    settled = settleQuoteLines(settled);
  }
  const fix = ChangeSet.of(settled === after ? [] : minimalChange(after, settled), after.length);
  const selection = caretOffCards(
    fix.apply(transaction.newDoc),
    transaction.newSelection.map(fix),
    transaction.startState.selection.main.head,
  );
  if (fix.empty && selection.eq(transaction.newSelection)) return transaction;
  return [transaction, { changes: fix, selection, sequential: true }];
}

/** Reply quotes as cards, for an input whose document is a reply draft's; see replyDraftDocument. */
const replyQuoteCards: Extension = [
  quoteCardField,
  EditorState.changeFilter.of(keepQuoteTokensWhole),
  EditorState.transactionFilter.of(settleQuoteCards),
  // No readline kill reaches into a card or the line breaks beside it.
  killStops.of((state) => {
    const stops: { from: number; to: number }[] = [];
    state.field(quoteCardField).between(0, state.doc.length, (from, to) => {
      stops.push({ from: Math.max(0, from - 1), to: Math.min(state.doc.length, to + 1) });
    });
    return stops;
  }),
  EditorView.clipboardOutputFilter.of((text) => quotesAsMarkdown(text)),
  // Without it a long card stretches the content past the editor, and its X out of view.
  EditorView.theme({ ".cm-content": { minWidth: "0" } }),
];

/** Whether `binding` claims a chord with both Control and Option, which VoiceOver owns. */
function bindsControlOption(binding: KeyBinding): boolean {
  return [binding.key, binding.mac, binding.win, binding.linux].some(
    (key) => key !== undefined && /(^|-)Ctrl-/.test(key) && /(^|-)Alt-/.test(key),
  );
}

/**
 * CodeMirror's editing keys, minus two kinds. Enter would insert a newline before the composer
 * decides to send; an unclaimed Enter still inserts one through the browser's own input
 * handling. And no Control+Option chord, such as the macOS Emacs keymap's ⌃⌥H, is claimed.
 */
export const COMPOSER_EDITING_KEYMAP: readonly KeyBinding[] = [
  ...historyKeymap,
  ...standardKeymap,
].filter((binding) => binding.key !== "Enter" && !bindsControlOption(binding));

function theme(heightMode: ComposerInputHeightMode): Extension {
  const unbounded = heightMode === "unbounded";
  return EditorView.theme({
    "&": { font: "inherit", color: "inherit", backgroundColor: "transparent" },
    "&.cm-focused": { outline: "none" },
    ".cm-scroller": {
      fontFamily: "inherit",
      lineHeight: "inherit",
      overflowX: "hidden",
      overflowY: unbounded ? "visible" : "auto",
      maxHeight: unbounded ? "none" : `${COMPOSER_MAX_HEIGHT_PX}px`,
      scrollbarWidth: "thin",
      scrollbarColor: "color-mix(in srgb, var(--foreground) 28%, transparent) transparent",
    },
    "&[data-overflowing] .cm-scroller": { overscrollBehaviorY: "contain" },
    // A fade at the top edge says there is more draft above.
    "&[data-scrolled] .cm-scroller": {
      maskImage: "linear-gradient(to bottom, transparent, black 18px)",
    },
    ".cm-content": { padding: "0", caretColor: "currentColor", minHeight: "1lh" },
    ".cm-content:focus-visible": { outline: "none" },
    ".cm-line": { padding: "0" },
    ".cm-placeholder": { color: "var(--muted-foreground)" },
  });
}

/**
 * Whether an ancestor fieldset is disabled. A contenteditable ignores a disabled fieldset,
 * which a textarea honours, so the editor follows it. Tracks each fieldset's `disabled`.
 */
function useFieldsetDisabled(hostRef: RefObject<HTMLElement | null>): boolean {
  const [disabled, setDisabled] = useState(false);
  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const fieldsets: HTMLFieldSetElement[] = [];
    for (let node = host.parentElement; node; node = node.parentElement) {
      if (node instanceof HTMLFieldSetElement) fieldsets.push(node);
    }
    // Like a textarea, a disabled fieldset's first legend keeps its contents enabled.
    const sync = () =>
      setDisabled(
        fieldsets.some(
          (fieldset) =>
            fieldset.disabled && !fieldset.querySelector(":scope > legend")?.contains(host),
        ),
      );
    const observer = new MutationObserver(sync);
    for (const fieldset of fieldsets) {
      observer.observe(fieldset, { attributes: true, attributeFilter: ["disabled"] });
    }
    sync();
    return () => observer.disconnect();
  }, [hostRef]);
  return disabled;
}

function attributes(
  props: ComposerInputProps,
  environment: CodeMirrorComposerEnvironment,
): Extension {
  const locked = props.disabled === true || props.readOnly === true;
  return [
    EditorView.editable.of(props.disabled !== true),
    EditorState.readOnly.of(locked),
    EditorView.contentAttributes.of({
      [environment.inputAttribute]: "",
      // CodeMirror turns these off by default; a textarea keeps the browser's behaviour.
      spellcheck: "true",
      autocorrect: "on",
      autocapitalize: "sentences",
      "aria-disabled": props.disabled ? "true" : "false",
      ...(props.readOnly ? { "aria-readonly": "true" } : {}),
      // A textarea is named by its placeholder when it has no label; the editor says so itself.
      ...(props["aria-label"] || props.placeholder
        ? { "aria-label": props["aria-label"] || props.placeholder! }
        : {}),
      ...(props.placeholder ? { "aria-placeholder": props.placeholder } : {}),
      ...(props["aria-keyshortcuts"] ? { "aria-keyshortcuts": props["aria-keyshortcuts"] } : {}),
      ...(props["aria-describedby"] ? { "aria-describedby": props["aria-describedby"] } : {}),
      ...(props.role ? { role: props.role } : {}),
      ...(props["aria-autocomplete"] ? { "aria-autocomplete": props["aria-autocomplete"] } : {}),
      ...(props["aria-controls"] ? { "aria-controls": props["aria-controls"] } : {}),
      ...(props["aria-expanded"] !== undefined
        ? { "aria-expanded": String(props["aria-expanded"]) }
        : {}),
      ...(props["aria-activedescendant"]
        ? { "aria-activedescendant": props["aria-activedescendant"] }
        : {}),
      ...(props.id ? { id: props.id } : {}),
      ...(props.inputId ? { [environment.inputIdAttribute]: props.inputId } : {}),
      ...(props["data-testid"] ? { "data-testid": props["data-testid"] } : {}),
      ...(props["data-has-draft"] ? { "data-has-draft": props["data-has-draft"] } : {}),
      ...(props["data-slash-command"] ? { "data-slash-command": props["data-slash-command"] } : {}),
      ...(props["data-shell-command"] ? { "data-shell-command": props["data-shell-command"] } : {}),
    }),
  ];
}

/** Line breaks as the document stores them, so a value measures the same as its document. */
function normalizeLineBreaks(text: string): string {
  return text.replace(/\r\n?/g, "\n");
}

/** The smallest change turning `current` into `next`, so undo history and mapped positions stay stable. */
function minimalChange(current: string, next: string) {
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

/** The surface shared composer logic edits, over one live EditorView. */
function createSurface(view: EditorView): ComposerEditorSurface {
  const clamp = (offset: number) => Math.max(0, Math.min(offset, view.state.doc.length));
  return {
    element: view.contentDOM,
    get isConnected() {
      return view.dom.isConnected;
    },
    get value() {
      return view.state.doc.toString();
    },
    get selectionStart() {
      return view.state.selection.main.from;
    },
    get selectionEnd() {
      return view.state.selection.main.to;
    },
    get scrollTop() {
      return view.scrollDOM.scrollTop;
    },
    set scrollTop(value) {
      view.scrollDOM.scrollTop = value;
    },
    get scrollLeft() {
      return view.scrollDOM.scrollLeft;
    },
    set scrollLeft(value) {
      view.scrollDOM.scrollLeft = value;
    },
    setSelectionRange(start, end) {
      // As a textarea does: a start past the end collapses to the end.
      const to = clamp(end);
      view.dispatch({ selection: EditorSelection.single(Math.min(clamp(start), to), to) });
    },
    focus() {
      // Not contentDOM.focus(): the browser would put its caret at the start and the editor
      // would take that as the user's selection. view.focus() keeps the editor's selection.
      view.focus();
    },
    blur() {
      view.contentDOM.blur();
    },
    insertText(text, start, end, options) {
      // Shared composer edits (paste, ⌥↵, list continuation) never change a locked draft.
      if (view.state.readOnly) return false;
      const from = clamp(start);
      const insert = normalizeLineBreaks(text);
      view.dispatch({
        changes: { from, to: clamp(end), insert },
        selection: { anchor: from + insert.length },
        userEvent: "input",
        // Each programmatic insert is its own undo step, not joined to the typing around it.
        annotations:
          options?.history === false
            ? Transaction.addToHistory.of(false)
            : isolateHistory.of("full"),
      });
      return true;
    },
    revealCaret() {
      view.dispatch({ effects: EditorView.scrollIntoView(view.state.selection.main.head) });
    },
  };
}

/** The CodeMirror composer input for `environment`; the loader makes one per page. */
export function createCodeMirrorComposerInput(environment: CodeMirrorComposerEnvironment) {
  return forwardRef<ComposerTextTarget, Omit<ComposerInputProps, "ref">>(
    function CodeMirrorComposerInput(props, forwardedRef) {
      return (
        <CodeMirrorComposer {...props} environment={environment} forwardedRef={forwardedRef} />
      );
    },
  );
}

/**
 * Bring a controlled value in without echoing a change or adding an undo step. A replaced value
 * also drops the undo history, as a textarea's value setter does, so undo can't restore old text.
 */
function syncControlledValue(view: EditorView, value: string, historyCompartment: Compartment) {
  const next = normalizeLineBreaks(value);
  const current = view.state.doc.toString();
  if (current === next) return;
  view.dispatch({
    changes: minimalChange(current, next),
    // As a textarea's value setter does: a new value puts the caret at its end.
    selection: EditorSelection.cursor(next.length),
    annotations: [controlledUpdate.of(true), Transaction.addToHistory.of(false)],
    effects: historyCompartment.reconfigure([]),
  });
  view.dispatch({ effects: historyCompartment.reconfigure(history()) });
}

function CodeMirrorComposer({
  environment,
  forwardedRef,
  ...props
}: Omit<ComposerInputProps, "ref"> & {
  environment: CodeMirrorComposerEnvironment;
  forwardedRef: Ref<ComposerTextTarget>;
}) {
  const {
    value,
    placeholder,
    autoFocus,
    heightMode = "compact",
    className,
    mentionTokens,
    replyQuotes,
  } = props;
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const surfaceRef = useRef<ComposerEditorSurface | null>(null);
  const latest = useRef(props);
  const fieldsetDisabled = useFieldsetDisabled(hostRef);
  const disabled = props.disabled === true || fieldsetDisabled;
  useLayoutEffect(() => {
    latest.current = props;
  });
  const compartments = useMemo(
    () => ({
      attributes: new Compartment(),
      history: new Compartment(),
      placeholder: new Compartment(),
      theme: new Compartment(),
      mentionTokens: new Compartment(),
      commandToken: new Compartment(),
      replyQuotes: new Compartment(),
    }),
    [],
  );

  // The editor is created once; the effects below reconfigure it.
  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const event = (): ComposerInputEvent => ({ currentTarget: surfaceRef.current! });
    const initial = latest.current;
    const view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: initial.value ?? initial.defaultValue ?? "",
        extensions: [
          EditorState.allowMultipleSelections.of(false),
          // Composer keys run first; CodeMirror's own bindings only see keys they pass on.
          Prec.highest(
            EditorView.domEventHandlers({
              keydown(keyEvent, editor) {
                if (keyEvent.isComposing || editor.composing || keyEvent.keyCode === 229) {
                  return false;
                }
                const composerEvent: ComposerKeyboardEvent = {
                  key: keyEvent.key,
                  code: keyEvent.code,
                  altKey: keyEvent.altKey,
                  ctrlKey: keyEvent.ctrlKey,
                  metaKey: keyEvent.metaKey,
                  shiftKey: keyEvent.shiftKey,
                  repeat: keyEvent.repeat,
                  get defaultPrevented() {
                    return keyEvent.defaultPrevented;
                  },
                  nativeEvent: keyEvent,
                  currentTarget: surfaceRef.current!,
                  preventDefault: () => keyEvent.preventDefault(),
                  stopPropagation: () => keyEvent.stopPropagation(),
                };
                latest.current.onKeyDown?.(composerEvent);
                return keyEvent.defaultPrevented;
              },
              paste(pasteEvent) {
                if (!pasteEvent.clipboardData) return false;
                latest.current.onPaste?.({
                  clipboardData: pasteEvent.clipboardData,
                  currentTarget: surfaceRef.current!,
                  preventDefault: () => pasteEvent.preventDefault(),
                });
                return pasteEvent.defaultPrevented;
              },
              focus() {
                latest.current.onFocus?.(event());
                return false;
              },
              blur() {
                latest.current.onBlur?.(event());
                return false;
              },
              compositionstart() {
                latest.current.onCompositionStart?.();
                return false;
              },
              compositionend() {
                latest.current.onCompositionEnd?.();
                return false;
              },
            }),
          ),
          readlineKeys(environment.mac),
          keymap.of(COMPOSER_EDITING_KEYMAP),
          compartments.history.of(history()),
          EditorView.lineWrapping,
          EditorState.transactionFilter.of((transaction) =>
            adjustedInsertTransaction(transaction, environment.separateMentionEdges),
          ),
          compartments.attributes.of(attributes(initial, environment)),
          compartments.placeholder.of(placeholderText(initial.placeholder ?? "")),
          compartments.theme.of(theme(initial.heightMode ?? "compact")),
          compartments.mentionTokens.of(mentionTokensFacet.of(initial.mentionTokens ?? [])),
          mentionTokenField(environment),
          compartments.commandToken.of(commandTokenFacet.of(documentCommandToken(initial))),
          commandTokenField(environment),
          compartments.replyQuotes.of(initial.replyQuotes ? replyQuoteCards : []),
          EditorView.updateListener.of((update) => {
            const fromUser = !update.transactions.some((transaction) =>
              transaction.annotation(controlledUpdate),
            );
            if (update.docChanged && fromUser) {
              const historyDirection = historyDirectionOfTransactions(update.transactions);
              latest.current.onChange?.({ ...event(), historyDirection });
            }
            if ((update.docChanged || update.selectionSet) && fromUser) {
              latest.current.onSelect?.(event());
            }
            if (update.docChanged || update.geometryChanged) markOverflow(update.view);
          }),
        ],
      }),
    });
    const markOverflow = (editor: EditorView) => {
      const scroller = editor.scrollDOM;
      editor.dom.toggleAttribute("data-overflowing", scroller.scrollHeight > scroller.clientHeight);
      editor.dom.toggleAttribute("data-scrolled", scroller.scrollTop > 0);
    };
    const onScroll = () => {
      markOverflow(view);
      latest.current.onScroll?.(event());
    };
    view.scrollDOM.addEventListener("scroll", onScroll, { passive: true });
    const surface = createSurface(view);
    viewRef.current = view;
    surfaceRef.current = surface;
    const unregister = environment.registerSurface(surface);
    return () => {
      unregister();
      view.scrollDOM.removeEventListener("scroll", onScroll);
      view.destroy();
      viewRef.current = null;
      surfaceRef.current = null;
    };
  }, [compartments, environment]);
  // React re-attaches the surface when the forwarded ref changes, as for a textarea.
  useImperativeHandle(forwardedRef, () => surfaceRef.current!, []);

  // Reconciles when `value` changes, not on every render: a render that lands before a pending
  // keystroke would otherwise revert it. So an edit the parent rejects without changing `value` stays.
  useLayoutEffect(() => {
    const view = viewRef.current;
    if (view && value !== undefined) syncControlledValue(view, value, compartments.history);
  }, [compartments, value]);

  // One key for every prop that becomes an editor or content attribute.
  const attributeKey = JSON.stringify([
    props.placeholder,
    disabled,
    props.readOnly,
    props["aria-label"],
    props["aria-keyshortcuts"],
    props["aria-describedby"],
    props.role,
    props["aria-autocomplete"],
    props["aria-controls"],
    props["aria-expanded"],
    props["aria-activedescendant"],
    props.id,
    props.inputId,
    props["data-testid"],
    props["data-has-draft"],
    props["data-slash-command"],
    props["data-shell-command"],
  ]);
  // In layout, so a draft that turns disabled or read-only takes no keys after the render.
  useLayoutEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: compartments.attributes.reconfigure(
        attributes({ ...latest.current, disabled }, environment),
      ),
    });
    // CodeMirror always marks its content aria-multiline, which a combobox doesn't take. It only
    // rewrites attributes whose value changed, so removing it here holds until the role changes.
    if (latest.current.role === "combobox") view.contentDOM.removeAttribute("aria-multiline");
    else view.contentDOM.setAttribute("aria-multiline", "true");
  }, [compartments, attributeKey, disabled, environment]);
  useEffect(() => {
    viewRef.current?.dispatch({
      effects: compartments.placeholder.reconfigure(placeholderText(placeholder ?? "")),
    });
  }, [compartments, placeholder]);
  useEffect(() => {
    viewRef.current?.dispatch({ effects: compartments.theme.reconfigure(theme(heightMode)) });
  }, [compartments, heightMode]);
  // Keyed by content, so a new array with the same tokens doesn't rebuild the marks.
  const mentionTokensKey = JSON.stringify(mentionTokens ?? []);
  useLayoutEffect(() => {
    viewRef.current?.dispatch({
      effects: compartments.mentionTokens.reconfigure(
        mentionTokensFacet.of(latest.current.mentionTokens ?? []),
      ),
    });
  }, [compartments, mentionTokensKey]);
  // Keyed by content, as a placed token is a new object each render.
  const commandTokenKey = JSON.stringify(documentCommandToken(props));
  useLayoutEffect(() => {
    viewRef.current?.dispatch({
      effects: compartments.commandToken.reconfigure(
        commandTokenFacet.of(documentCommandToken(latest.current)),
      ),
    });
  }, [compartments, commandTokenKey]);
  useLayoutEffect(() => {
    viewRef.current?.dispatch({
      effects: compartments.replyQuotes.reconfigure(replyQuotes ? replyQuoteCards : []),
    });
  }, [compartments, replyQuotes]);
  useEffect(() => {
    if (autoFocus) viewRef.current?.focus();
  }, [autoFocus]);

  return (
    <div
      ref={hostRef}
      data-composer-engine="codemirror"
      // A caller's text size replaces the default one, as the floating panel's does.
      className={cn(
        "composer-input-text relative w-full text-ui text-foreground md:min-h-[42px]",
        disabled && "opacity-60",
        className,
      )}
    />
  );
}
