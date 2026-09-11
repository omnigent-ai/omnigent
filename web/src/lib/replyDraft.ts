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

function joinParagraphs(parts: string[]): string {
  return parts
    .map((part) => part.replace(/^\n+|\n+$/g, ""))
    .filter((part) => part.trim() !== "")
    .join("\n\n");
}

export function serializeReplyDraft(draft: ReplyDraft): string {
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

/** Restore quote cards from saved, recalled, queued, or failed-send Markdown. */
export function parseReplyDraft(text: string): ReplyDraft {
  const quotes: ReplyQuote[] = [];
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let pending: string[] = [];
  let fence: { marker: string; length: number } | null = null;
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index]!;
    const fenceMatch = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (fenceMatch) {
      const marker = fenceMatch[1]!;
      if (!fence) fence = { marker: marker[0]!, length: marker.length };
      else if (marker[0] === fence.marker && marker.length >= fence.length) fence = null;
    }
    if (!fence && /^>( |$)/.test(line)) {
      const quoted = [line.replace(/^> ?/, "")];
      while (index + 1 < lines.length && /^>( |$)/.test(lines[index + 1]!)) {
        quoted.push(lines[++index]!.replace(/^> ?/, ""));
      }
      quotes.push({
        id: nanoid(),
        before: pending.join("\n").replace(/^\n+|\n+$/g, ""),
        text: quoted.join("\n"),
      });
      pending = [];
    } else {
      pending.push(line);
    }
  }
  return {
    quotes,
    text: quotes.length ? pending.join("\n").replace(/^\n+/, "") : text,
  };
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
