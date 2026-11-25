// A reply draft as one editor document. Each quote is a token line that the CodeMirror composer
// paints as a card. Tokens are editor-only: the draft that is sent and stored never holds one.

import type { ReplyDraft, ReplyQuote } from "./replyDraft";

/** Opens a token. Typed text never carries it, so a token can only come from a quote. */
export const REPLY_QUOTE_MARK = "\uE000";
/** Closes a token. */
export const REPLY_QUOTE_END = "\uE001";

type QuoteFields = Pick<ReplyQuote, "id" | "text">;

const BASE64URL_PATTERN = /^[A-Za-z0-9_-]*$/;
const TOKEN_PATTERN = new RegExp(`${REPLY_QUOTE_MARK}[A-Za-z0-9_-]*${REPLY_QUOTE_END}`, "g");
const MARK_PATTERN = new RegExp(`[${REPLY_QUOTE_MARK}${REPLY_QUOTE_END}]`, "g");

function encodePayload(payload: string): string {
  let binary = "";
  for (const byte of new TextEncoder().encode(payload)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodePayload(encoded: string): string | null {
  if (!BASE64URL_PATTERN.test(encoded)) return null;
  try {
    const base64 = encoded.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(base64 + "=".repeat((4 - (base64.length % 4)) % 4));
    return new TextDecoder("utf-8", { fatal: true }).decode(
      Uint8Array.from(binary, (character) => character.charCodeAt(0)),
    );
  } catch {
    return null;
  }
}

/** The token line for `quote`. */
export function replyQuoteLine(quote: QuoteFields): string {
  return REPLY_QUOTE_MARK + encodePayload(JSON.stringify([quote.id, quote.text])) + REPLY_QUOTE_END;
}

/** The quote a document line holds, or null for any other line, a damaged token included. */
export function parseReplyQuoteLine(line: string): QuoteFields | null {
  if (!line.startsWith(REPLY_QUOTE_MARK) || !line.endsWith(REPLY_QUOTE_END)) return null;
  const payload = decodePayload(line.slice(REPLY_QUOTE_MARK.length, -REPLY_QUOTE_END.length));
  if (payload === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return null;
  }
  if (
    !Array.isArray(parsed) ||
    parsed.length !== 2 ||
    typeof parsed[0] !== "string" ||
    typeof parsed[1] !== "string"
  )
    return null;
  return { id: parsed[0], text: parsed[1] };
}

/** The [start, end) ranges of the whole tokens in `text`. */
export function replyQuoteTokenRanges(text: string): { start: number; end: number }[] {
  return Array.from(text.matchAll(TOKEN_PATTERN), (match) => ({
    start: match.index ?? 0,
    end: (match.index ?? 0) + match[0].length,
  }));
}

/** `text` without token marks, so plain text can't pose as a quote. */
export function withoutReplyQuoteMarks(text: string): string {
  return text.replace(TOKEN_PATTERN, "").replace(MARK_PATTERN, "");
}

/**
 * The document for `draft`. A first quote with no text before it gets no line above its card, so
 * the document opens on the card. Later quotes keep their empty line, as the textarea keeps its
 * empty input.
 */
export function weaveReplyDraft(draft: ReplyDraft): string {
  const lines: string[] = [];
  draft.quotes.forEach((quote, index) => {
    if (index > 0 || quote.before !== "") lines.push(withoutReplyQuoteMarks(quote.before));
    lines.push(replyQuoteLine(quote));
  });
  lines.push(withoutReplyQuoteMarks(draft.text));
  return lines.join("\n");
}

/**
 * The draft a document holds. A repeated token id gets a suffix, so every quote has its own id
 * and parsing the same document always gives the same draft.
 */
export function unweaveReplyDraft(document: string): ReplyDraft {
  const ids = new Set<string>();
  const quotes: ReplyQuote[] = [];
  let pending: string[] = [];
  for (const line of document.split("\n")) {
    const quote = parseReplyQuoteLine(line);
    if (!quote) {
      // A stray mark in plain text can't pose as a card, so it goes with the text.
      pending.push(withoutReplyQuoteMarks(line));
      continue;
    }
    let id = quote.id;
    for (let n = 2; ids.has(id); n += 1) id = `${quote.id}-${n}`;
    ids.add(id);
    quotes.push({ id, before: pending.join("\n"), text: quote.text });
    pending = [];
  }
  return { quotes, text: pending.join("\n") };
}

/** Whether two drafts hold the same quotes and text, ids included. */
export function sameReplyDraft(a: ReplyDraft, b: ReplyDraft): boolean {
  return (
    a.text === b.text &&
    a.quotes.length === b.quotes.length &&
    a.quotes.every((quote, index) => {
      const other = b.quotes[index]!;
      return quote.id === other.id && quote.before === other.before && quote.text === other.text;
    })
  );
}

/**
 * The document that shows `draft`: the typed document while it still describes the draft, so
 * edits such as a deleted blank line survive. After a change made in code, the woven draft.
 */
export function typedDocumentFor(draft: ReplyDraft, typed: string | null | undefined): string {
  if (typed != null && hasOnlyWholeCards(typed) && sameReplyDraft(unweaveReplyDraft(typed), draft))
    return typed;
  return weaveReplyDraft(draft);
}

/** Whether every mark in `document` sits in a whole, well-formed card line. */
function hasOnlyWholeCards(document: string): boolean {
  return document
    .split("\n")
    .every((line) => !hasReplyQuoteMark(line) || parseReplyQuoteLine(line) !== null);
}

function hasReplyQuoteMark(text: string): boolean {
  return text.includes(REPLY_QUOTE_MARK) || text.includes(REPLY_QUOTE_END);
}

/** The document with `quote` added as the newest card, after the text that ends it. */
export function appendQuoteToDocument(document: string, quote: QuoteFields): string {
  const line = replyQuoteLine(quote);
  return document === "" ? `${line}\n` : `${document}\n${line}\n`;
}

/**
 * The lines a card's removal takes, as `first` to `last` line indices. The card's own line goes,
 * and so does the blank line above it when that blank only separates the card from the document's
 * start or from another card. Every other line stays.
 */
export function quoteRemovalSpan(
  lines: readonly string[],
  index: number,
): { first: number; last: number } {
  const separator =
    index >= 1 &&
    lines[index - 1] === "" &&
    (index === 1 || parseReplyQuoteLine(lines[index - 2]!) !== null);
  return { first: separator ? index - 1 : index, last: index };
}

/** The document without the card on line `lineIndex`, by `quoteRemovalSpan`. */
export function removeReplyQuoteLine(document: string, lineIndex: number): string {
  const lines = document.split("\n");
  if (parseReplyQuoteLine(lines[lineIndex] ?? "") === null) return document;
  const { first, last } = quoteRemovalSpan(lines, lineIndex);
  lines.splice(first, last - first + 1);
  return lines.join("\n");
}

/** The document without its `index`-th card, which is the card at that position in the draft. */
export function removeReplyQuoteAt(document: string, index: number): string {
  let seen = -1;
  const lineIndex = document.split("\n").findIndex((line) => {
    if (parseReplyQuoteLine(line) === null) return false;
    seen += 1;
    return seen === index;
  });
  return lineIndex < 0 ? document : removeReplyQuoteLine(document, lineIndex);
}

/**
 * A document with the editor's rules applied to an edit made in code. Only a token that `previous`
 * already holds stays a card, so a value from code can't forge one; any other mark is removed with
 * its payload. Text beside a card goes on its own line, and a card is never the last line.
 */
export function settleReplyDocument(document: string, previous: string): string {
  const known = new Set(
    replyQuoteTokenRanges(previous).map((token) => previous.slice(token.start, token.end)),
  );
  const lines: string[] = [];
  const pushText = (segment: string) => {
    const plain = withoutReplyQuoteMarks(segment);
    if (plain !== "") lines.push(plain);
  };
  for (const line of document.split("\n")) {
    if (!hasReplyQuoteMark(line)) {
      lines.push(line);
      continue;
    }
    // Text around a dropped token joins up, so a foreign token leaves its line whole.
    let text = "";
    let rest = 0;
    for (const token of replyQuoteTokenRanges(line)) {
      text += line.slice(rest, token.start);
      const card = line.slice(token.start, token.end);
      if (known.has(card)) {
        pushText(text);
        text = "";
        lines.push(card);
      }
      rest = token.end;
    }
    pushText(text + line.slice(rest));
  }
  if (lines.length > 0 && parseReplyQuoteLine(lines[lines.length - 1]!) !== null) lines.push("");
  return lines.join("\n");
}

/** Where a document's text sits in the composer's inputs: each card's text before it, then the tail. */
export interface ComposerTextRegion {
  /** The input that holds the text: a card's id, or the tail's. */
  inputId: string;
  start: number;
  end: number;
}

/**
 * The regions of `document`, in order. A card's own input is its `quotes[index].id`; the tail's is
 * `tailId`. Regions are positions in the document.
 */
export function composerTextRegions(
  document: string,
  quoteIds: readonly string[],
  tailId: string,
): ComposerTextRegion[] {
  const regions: ComposerTextRegion[] = [];
  let start = 0;
  let lineStart = 0;
  let index = 0;
  for (const line of document.split("\n")) {
    if (parseReplyQuoteLine(line) !== null) {
      regions.push({
        inputId: quoteIds[index] ?? tailId,
        start,
        end: Math.max(start, lineStart - 1),
      });
      index += 1;
      start = lineStart + line.length + 1;
    }
    lineStart += line.length + 1;
  }
  // A document that ends in a card has no text after it, so the tail starts at its end.
  regions.push({ inputId: tailId, start: Math.min(start, document.length), end: document.length });
  return regions;
}

/** A selection in one composer input, as the textarea and the editor each report it. */
export interface ComposerInputSelection {
  inputId: string;
  selectionStart: number;
  selectionEnd: number;
}

/**
 * A selection carried across an engine switch. From the textarea, an input's offsets become
 * document offsets; from the editor, a document offset becomes its input and offset. A selection
 * can't span two inputs, so an end in another input collapses to the start.
 */
export function mapComposerSelection(
  document: string,
  selection: ComposerInputSelection,
  from: "textarea" | "codemirror",
  quoteIds: readonly string[],
  tailId: string,
): ComposerInputSelection {
  const regions = composerTextRegions(document, quoteIds, tailId);
  if (from === "textarea") {
    const region = regions.find((candidate) => candidate.inputId === selection.inputId);
    // A card that is gone leaves the caret at the end of the tail.
    if (!region) {
      const tail = regions[regions.length - 1]!;
      return { inputId: tailId, selectionStart: tail.end, selectionEnd: tail.end };
    }
    const size = region.end - region.start;
    return {
      inputId: tailId,
      selectionStart: region.start + Math.min(selection.selectionStart, size),
      selectionEnd: region.start + Math.min(selection.selectionEnd, size),
    };
  }
  const locate = (offset: number) => {
    const region = regions.reduce<ComposerTextRegion | undefined>(
      (found, candidate) => (candidate.start <= offset ? candidate : found),
      undefined,
    );
    if (!region) return { inputId: tailId, offset: 0 };
    return { inputId: region.inputId, offset: Math.min(offset, region.end) - region.start };
  };
  const start = locate(selection.selectionStart);
  const end = locate(selection.selectionEnd);
  return {
    inputId: start.inputId,
    selectionStart: start.offset,
    selectionEnd: end.inputId === start.inputId ? end.offset : start.offset,
  };
}

/** A document's text as the clipboard gets it: each card becomes its `> ` markdown. */
export function replyQuoteClipboardText(text: string): string {
  return text
    .split("\n")
    .flatMap((line) => {
      const quote = parseReplyQuoteLine(line);
      return quote ? quote.text.split("\n").map((textLine) => `> ${textLine}`) : [line];
    })
    .join("\n");
}
