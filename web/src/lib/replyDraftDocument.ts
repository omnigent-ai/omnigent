// A reply draft as one editor document: each quote is a token line, so undo restores a quote
// with its text. The document is editor-only and never persisted.

import type { ReplyDraft, ReplyQuote } from "./replyDraft";

/** Opens a quote's token: a private-use character that typed text does not carry. */
export const REPLY_QUOTE_MARK = "\uE000";
/** Closes a quote's token. */
export const REPLY_QUOTE_END = "\uE001";

const BASE64URL_PATTERN = /^[A-Za-z0-9_-]*$/;
/** A whole token, or a stray mark: what plain text must not carry. */
const TOKEN_OR_MARK = new RegExp(
  `${REPLY_QUOTE_MARK}[A-Za-z0-9_-]*${REPLY_QUOTE_END}|${REPLY_QUOTE_MARK}|${REPLY_QUOTE_END}`,
  "g",
);

function encodePayload(payload: string): string {
  const bytes = new TextEncoder().encode(payload);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
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

type QuoteFields = Pick<ReplyQuote, "id" | "text">;

/** The token line for `quote`. */
export function replyQuoteLine(quote: QuoteFields): string {
  return REPLY_QUOTE_MARK + encodePayload(JSON.stringify([quote.id, quote.text])) + REPLY_QUOTE_END;
}

/** The quote a document line holds, or null for any other line, a damaged one included. */
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

/**
 * The document for `draft`: each quote on its own line, after the text before it, and the
 * trailing text last. A draft without quotes is its text, unchanged. Weaving can add a separator
 * line that a typed document lacks, so a typed document is kept as typed (see `typedDocumentFor`).
 */
export function weaveReplyDraft(draft: ReplyDraft): string {
  // Plain text never becomes a quote: a token in it, pasted into the textarea, loses its marks here.
  return [
    ...draft.quotes.flatMap((quote) => [
      withoutReplyQuoteMarks(quote.before),
      replyQuoteLine(quote),
    ]),
    withoutReplyQuoteMarks(draft.text),
  ].join("\n");
}

/**
 * The draft a document holds. A repeated quote id becomes `${id}-${n}` for the first free `n`, so
 * parsing the same document twice gives the same draft.
 */
export function unweaveReplyDraft(document: string): ReplyDraft {
  const lines = document.split("\n");
  const used = new Set(lines.flatMap((line) => parseReplyQuoteLine(line)?.id ?? []));
  const seen = new Set<string>();
  const quotes: ReplyQuote[] = [];
  let pending: string[] = [];
  for (const line of lines) {
    const quote = parseReplyQuoteLine(line);
    if (quote) {
      let id = quote.id;
      if (seen.has(id)) {
        let n = 2;
        while (used.has(`${quote.id}-${n}`)) n += 1;
        id = `${quote.id}-${n}`;
        used.add(id);
      }
      seen.add(id);
      quotes.push({ id, before: pending.join("\n"), text: quote.text });
      pending = [];
    } else {
      pending.push(line);
    }
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
 * The document that shows `draft`: the typed document while it still describes the draft, and
 * the woven draft otherwise, as after a change made in code.
 */
export function typedDocumentFor(draft: ReplyDraft, typed: string | null | undefined): string {
  if (typed != null && sameReplyDraft(unweaveReplyDraft(typed), draft)) return typed;
  return weaveReplyDraft(draft);
}

/** Whether a document holds any quote line. */
export function hasReplyQuoteLines(document: string): boolean {
  return document.split("\n").some((line) => parseReplyQuoteLine(line) !== null);
}

/** `text` without quote marks, so inserted text can't pose as a quote. */
export function withoutReplyQuoteMarks(text: string): string {
  return text.replace(TOKEN_OR_MARK, "");
}

/** A document's text as the clipboard gets it: each quote line becomes its `> ` markdown. */
export function replyQuoteClipboardText(text: string): string {
  return text
    .split("\n")
    .flatMap((line) => {
      const quote = parseReplyQuoteLine(line);
      return quote ? quote.text.split("\n").map((textLine) => `> ${textLine}`) : [line];
    })
    .join("\n");
}

/** The document with `quote` added as the newest one, after the trailing text. */
export function appendQuoteToDocument(document: string, quote: QuoteFields): string {
  return `${document}\n${replyQuoteLine(quote)}\n`;
}

/** The document with its trailing text replaced, keeping every line up to the last quote line. */
export function replaceTrailingText(document: string, text: string): string {
  const plain = withoutReplyQuoteMarks(text);
  const lines = document.split("\n");
  let last = -1;
  lines.forEach((line, index) => {
    if (parseReplyQuoteLine(line) !== null) last = index;
  });
  return last < 0 ? plain : [...lines.slice(0, last + 1), plain].join("\n");
}

/**
 * The lines a quote's removal takes, as `first` to `last` line indices. The quote's own line goes,
 * and the blank line above it too when that blank only separates the quote from the document's
 * start or from another quote. Every other line stays.
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

/** The document without the quote line at `lineIndex`, by `quoteRemovalSpan`. */
export function removeReplyQuoteLine(document: string, lineIndex: number): string {
  const lines = document.split("\n");
  if (parseReplyQuoteLine(lines[lineIndex] ?? "") === null) return document;
  const { first, last } = quoteRemovalSpan(lines, lineIndex);
  lines.splice(first, last - first + 1);
  return lines.join("\n");
}

/** The line index of the quote with `id`, or -1. */
export function replyQuoteLineIndex(document: string, id: string): number {
  return document.split("\n").findIndex((line) => parseReplyQuoteLine(line)?.id === id);
}

/** An input of the composer and a selection within it, as the two engines report them. */
export interface ComposerSelection {
  inputId: string;
  selectionStart: number;
  selectionEnd: number;
}

interface InputRegion {
  inputId: string;
  start: number;
  end: number;
}

/** Where each input's text sits in the document: each quote's text before it, then the trailing text. */
function inputRegions(document: string, tailId: string): InputRegion[] {
  const regions: InputRegion[] = [];
  let start = 0;
  let lineStart = 0;
  for (const line of document.split("\n")) {
    const quote = parseReplyQuoteLine(line);
    if (quote) {
      regions.push({ inputId: quote.id, start, end: Math.max(start, lineStart - 1) });
      start = lineStart + line.length + 1;
    }
    lineStart += line.length + 1;
  }
  regions.push({ inputId: tailId, start, end: document.length });
  return regions;
}

/**
 * A selection carried across an engine switch. From a textarea, its input's offsets become
 * document offsets; from the editor, a document offset becomes its input and offset. An offset in
 * a quote's own line lands at the end of the text before it.
 */
export function mapComposerSelection(
  document: string,
  selection: ComposerSelection,
  from: "textarea" | "codemirror",
  tailId: string,
): ComposerSelection {
  const regions = inputRegions(document, tailId);
  if (from === "textarea") {
    const region = regions.find((candidate) => candidate.inputId === selection.inputId);
    if (!region) return selection;
    const size = region.end - region.start;
    return {
      inputId: tailId,
      selectionStart: region.start + Math.min(selection.selectionStart, size),
      selectionEnd: region.start + Math.min(selection.selectionEnd, size),
    };
  }
  const locate = (offset: number) => {
    const region = regions.reduce<InputRegion | undefined>(
      (found, candidate) => (candidate.start <= offset ? candidate : found),
      undefined,
    );
    if (!region) return { inputId: tailId, offset: 0 };
    return { inputId: region.inputId, offset: Math.min(offset, region.end) - region.start };
  };
  const start = locate(selection.selectionStart);
  const end = locate(selection.selectionEnd);
  // A textarea selection can't span two inputs, so an end in another input collapses to the start.
  return {
    inputId: start.inputId,
    selectionStart: start.offset,
    selectionEnd: end.inputId === start.inputId ? end.offset : start.offset,
  };
}
