// A reply draft as one editor document. Each quote is one line that carries its id and text,
// so the document alone holds the quote: undo brings a removed quote back with its text, and
// nothing is parsed back out of the markdown, where a typed `>` would look like a quote.

import type { ReplyDraft, ReplyQuote } from "./replyDraft";

/** Opens a quote's line. A private-use character that typed text does not carry. */
export const REPLY_QUOTE_MARK = "";

const BASE64URL_PATTERN = /^[A-Za-z0-9_-]*$/;

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

/** The line for `quote`, which a document keeps as its own line. */
function quoteLine(quote: Pick<ReplyQuote, "id" | "text">): string {
  return REPLY_QUOTE_MARK + encodePayload(JSON.stringify([quote.id, quote.text]));
}

/** The quote a document line holds, or null for any other line (including a damaged one). */
export function parseReplyQuoteLine(line: string): Pick<ReplyQuote, "id" | "text"> | null {
  if (!line.startsWith(REPLY_QUOTE_MARK)) return null;
  const payload = decodePayload(line.slice(REPLY_QUOTE_MARK.length));
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
 * trailing text last. A draft without quotes is its text, unchanged.
 */
export function weaveReplyDraft(draft: ReplyDraft): string {
  return [...draft.quotes.flatMap((quote) => [quote.before, quoteLine(quote)]), draft.text].join(
    "\n",
  );
}

/**
 * The draft a document holds. Round trips are not exact: a document can hold a blank line that
 * `weaveReplyDraft` would not write, so keep the document as the source of truth while editing.
 */
export function unweaveReplyDraft(document: string): ReplyDraft {
  const quotes: ReplyQuote[] = [];
  let pending: string[] = [];
  for (const line of document.split("\n")) {
    const quote = parseReplyQuoteLine(line);
    if (quote) {
      quotes.push({ ...quote, before: pending.join("\n") });
      pending = [];
    } else {
      pending.push(line);
    }
  }
  return { quotes, text: pending.join("\n") };
}

/** Whether a document holds any quote line. */
export function hasReplyQuoteLines(document: string): boolean {
  return document.split("\n").some((line) => parseReplyQuoteLine(line) !== null);
}

/** `text` without quote marks, so a pasted line can't pose as a quote. */
export function withoutReplyQuoteMarks(text: string): string {
  return text.split(REPLY_QUOTE_MARK).join("");
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
