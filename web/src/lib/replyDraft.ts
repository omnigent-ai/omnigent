import { nanoid } from "nanoid";

export interface ReplyQuote {
  id: string;
  before: string;
  text: string;
}

export interface ReplyDraft {
  quotes: ReplyQuote[];
  text: string;
}

export interface StoredReplyDraft {
  version: 1;
  quotes: Omit<ReplyQuote, "id">[];
  text: string;
}

export interface ComposerDraft {
  text: string;
  replyDraft?: StoredReplyDraft;
}

function joinParagraphs(parts: string[]): string {
  return parts.reduce((joined, part) => {
    if (!joined || !part) return joined + part;
    const trailing = joined.match(/(?:\r?\n){1,2}$/)?.[0].match(/\n/g)?.length ?? 0;
    const leading = part.match(/^(?:\r?\n){1,2}/)?.[0].match(/\n/g)?.length ?? 0;
    return joined + "\n".repeat(Math.max(0, 2 - trailing - leading)) + part;
  }, "");
}

export function serializeReplyDraft(draft: Omit<StoredReplyDraft, "version">): string {
  if (draft.quotes.length === 0) return draft.text;
  return joinParagraphs([
    ...draft.quotes.flatMap((quote) => [
      quote.before,
      quote.text
        .split("\n")
        .map((line) => `> ${line}`)
        .join("\n"),
    ]),
    draft.text,
  ]);
}

export function snapshotReplyDraft(draft: ReplyDraft): StoredReplyDraft | undefined {
  if (draft.quotes.length === 0) return undefined;
  return {
    version: 1,
    quotes: draft.quotes.map(({ before, text }) => ({ before, text })),
    text: draft.text,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Unannotated or unsupported drafts remain plain text, never inferred cards. */
export function readComposerDraft(value: unknown): ComposerDraft | undefined {
  if (typeof value === "string") return { text: value };
  if (!isRecord(value) || typeof value.text !== "string") return undefined;
  const plain = { text: value.text };
  const saved = value.replyDraft;
  if (
    !isRecord(saved) ||
    saved.version !== 1 ||
    typeof saved.text !== "string" ||
    !Array.isArray(saved.quotes) ||
    saved.quotes.length === 0
  )
    return plain;
  const quotes: StoredReplyDraft["quotes"] = [];
  for (const quote of saved.quotes) {
    if (!isRecord(quote) || typeof quote.before !== "string" || typeof quote.text !== "string")
      return plain;
    quotes.push({ before: quote.before, text: quote.text });
  }
  const replyDraft: StoredReplyDraft = { version: 1, quotes, text: saved.text };
  return serializeReplyDraft(replyDraft) === value.text ? { ...plain, replyDraft } : plain;
}

export function restoreReplyDraft(text: string, replyDraft?: StoredReplyDraft): ReplyDraft {
  const saved = readComposerDraft({ text, replyDraft })?.replyDraft;
  return saved
    ? { quotes: saved.quotes.map((quote) => ({ ...quote, id: nanoid() })), text: saved.text }
    : { quotes: [], text };
}

export function removeReplyQuote(draft: ReplyDraft, id: string): ReplyDraft {
  const index = draft.quotes.findIndex((quote) => quote.id === id);
  if (index < 0) return draft;
  const quotes = [...draft.quotes];
  const [removed] = quotes.splice(index, 1);
  const next = quotes[index];
  if (next) {
    quotes[index] = { ...next, before: joinParagraphs([removed!.before, next.before]) };
    return { ...draft, quotes };
  }
  return { quotes, text: joinParagraphs([removed!.before, draft.text]) };
}
