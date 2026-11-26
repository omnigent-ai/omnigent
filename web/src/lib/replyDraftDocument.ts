// A reply draft as one editor document, for the CodeMirror composer. Each quote is a line holding
// a token that the editor paints as a card. The document is editor-only: nothing stores or sends it.

import { nanoid } from "nanoid";
import type { ReplyDraft, ReplyQuote } from "./replyDraft";

// A token is U+FDD0, base64url JSON `[id, text]`, then U+FDD1. These are noncharacters, which
// no font or paste uses; icon fonts use the private-use area.
const TOKEN = /\uFDD0[\w-]*\uFDD1/g;
const TOKEN_SPLIT = /(\uFDD0[\w-]*\uFDD1)/;
const TOKEN_LINE = /^\uFDD0[\w-]*\uFDD1$/;
const MARK = /[\uFDD0\uFDD1]/;
const MARKS = /[\uFDD0\uFDD1]/g;

function encode(payload: string): string {
  let binary = "";
  for (const byte of new TextEncoder().encode(payload)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decode(encoded: string): string {
  const binary = atob(encoded.replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}

/** The token line for a quote. */
export function quoteLine(id: string, text: string): string {
  return `\uFDD0${encode(JSON.stringify([id, text]))}\uFDD1`;
}

/** The quote a document line holds, or null for any other line, a damaged token included. */
export function parseQuoteLine(line: string): Pick<ReplyQuote, "id" | "text"> | null {
  if (!TOKEN_LINE.test(line)) return null;
  try {
    const parsed: unknown = JSON.parse(decode(line.slice(1, -1)));
    if (!Array.isArray(parsed) || parsed.length !== 2) return null;
    const [id, text] = parsed as unknown[];
    return typeof id === "string" && typeof text === "string" ? { id, text } : null;
  } catch {
    return null;
  }
}

export function hasQuoteMark(text: string): boolean {
  return MARK.test(text);
}

/** `text` without tokens or stray marks, so plain text can't pose as a quote. */
export function stripQuoteMarks(text: string): string {
  return text.replace(TOKEN, "").replace(MARKS, "");
}

/** The document for `draft`: each quote's text before it, its token line, then the tail. */
export function weaveReplyDraft(draft: ReplyDraft): string {
  const lines = draft.quotes.flatMap((quote, index) => {
    const line = quoteLine(quote.id, quote.text);
    // As in the textarea, a first quote with no text before it opens the draft.
    return index === 0 && quote.before === "" ? [line] : [stripQuoteMarks(quote.before), line];
  });
  return [...lines, stripQuoteMarks(draft.text)].join("\n");
}

/** The draft `document` holds. Only a whole token line is a quote; other marks are dropped. */
export function unweaveReplyDraft(document: string): ReplyDraft {
  const quotes: ReplyQuote[] = [];
  let text: string[] = [];
  for (const line of document.split("\n")) {
    const quote = parseQuoteLine(line);
    if (!quote) {
      text.push(stripQuoteMarks(line));
      continue;
    }
    quotes.push({ ...quote, before: text.join("\n") });
    text = [];
  }
  return { quotes, text: text.join("\n") };
}

/** `document` with each token on a line of its own, and a text line after the last card. */
export function settleQuoteLines(document: string): string {
  const lines = document
    .split("\n")
    .flatMap((line) =>
      hasQuoteMark(line) ? line.split(TOKEN_SPLIT).filter((part) => part !== "") : [line],
    );
  if (parseQuoteLine(lines.at(-1) ?? "") !== null) lines.push("");
  return lines.join("\n");
}

/** Where `document`'s text starts after the cards that open it: the top a caret reaches. */
export function quoteTextStart(document: string): number {
  let offset = 0;
  for (const line of document.split("\n")) {
    if (parseQuoteLine(line) === null) break;
    offset += line.length + 1;
  }
  return offset;
}

/** `document` with its tail, the text after the last card, replaced by `tail`. Every card stays. */
export function replaceQuoteTail(document: string, tail: string): string {
  const lines = document.split("\n");
  const lastCard = lines.findLastIndex((line) => parseQuoteLine(line) !== null);
  return [...lines.slice(0, lastCard + 1), stripQuoteMarks(tail)].join("\n");
}

/** `document` with a new quote after it: the tail becomes the quote's text before it. */
export function appendQuoteToDocument(document: string, text: string): string {
  const line = quoteLine(nanoid(), text.replace(/\r\n?/g, "\n"));
  return document === "" ? `${line}\n` : `${document}\n${line}\n`;
}

/**
 * What removing quote `id` takes: its line, and a blank line beside it that only separates it
 * from another card or the document's edge. The text around it stays as shown.
 */
export function quoteRemovalRange(
  document: string,
  id: string,
): { from: number; to: number } | null {
  const lines = document.split("\n");
  const index = lines.findIndex((line) => parseQuoteLine(line)?.id === id);
  if (index < 0) return null;
  const separates = (blank: number, beyond: number) =>
    lines[blank] === "" &&
    (beyond < 0 || beyond >= lines.length || parseQuoteLine(lines[beyond]!) !== null);
  let [first, last] = [index, index];
  if (separates(index - 1, index - 2)) first -= 1;
  else if (separates(index + 1, index + 2)) last += 1;
  const start = (line: number) =>
    lines.slice(0, line).reduce((sum, text) => sum + text.length + 1, 0);
  // The lines go with the line break after them, or the one before them at the document's end.
  if (last + 1 < lines.length) return { from: start(first), to: start(last + 1) };
  return { from: Math.max(0, start(first) - 1), to: document.length };
}

/** `document` without quote `id`; see `quoteRemovalRange`. */
export function removeQuoteFromDocument(document: string, id: string): string {
  const range = quoteRemovalRange(document, id);
  if (!range) return document;
  return settleQuoteLines(document.slice(0, range.from) + document.slice(range.to));
}

/** `text` as the clipboard takes it: each quote line becomes its `> ` markdown. */
export function quotesAsMarkdown(text: string): string {
  return text
    .split("\n")
    .map((line) => {
      const quote = parseQuoteLine(line);
      if (!quote) return stripQuoteMarks(line);
      return quote.text
        .split("\n")
        .map((part) => `> ${part}`)
        .join("\n");
    })
    .join("\n");
}
