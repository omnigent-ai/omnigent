// What the Sessions finder previews for a session: the messages around the one that
// matches the query, cut to the part that holds the match.

import { foldText, type MatchRange, occurrenceRanges } from "@/components/ui/quick-pick";

export type TranscriptRole = "user" | "assistant";

export interface TranscriptMessage {
  role: TranscriptRole;
  text: string;
}

export interface PreviewMessage extends TranscriptMessage {
  /** Where the query occurs in `text`. */
  ranges: readonly MatchRange[];
  /** The message that matched, as opposed to the context around it. */
  matched: boolean;
  /** Where the message sits in the transcript, which names it within one excerpt. */
  position: number;
  /** Text was cut before or after the excerpt. */
  clippedStart: boolean;
  clippedEnd: boolean;
}

export interface SessionPreviewExcerpt {
  messages: PreviewMessage[];
  /** False when no loaded message holds the query, so the excerpt is the latest messages. */
  found: boolean;
}

/** Characters of a long message kept around its first match, or from its start. */
export const EXCERPT_CHARS = 480;
/** Messages shown either side of the matching one. */
const CONTEXT_MESSAGES = 1;
/** Messages shown when none matches. */
const LATEST_MESSAGES = 3;

const TEXT_BLOCK_TYPES = new Set(["input_text", "output_text", "text"]);

/** A user or agent message's text from one raw session item, or undefined for anything else. */
export function transcriptMessage(item: unknown): TranscriptMessage | undefined {
  if (item === null || typeof item !== "object") return undefined;
  const record = item as Record<string, unknown>;
  if (record.type !== "message") return undefined;
  if (record.role !== "user" && record.role !== "assistant") return undefined;
  const content = record.content;
  const parts: string[] = [];
  if (typeof content === "string") parts.push(content);
  else if (Array.isArray(content)) {
    for (const block of content) {
      if (block === null || typeof block !== "object") continue;
      const { type, text } = block as Record<string, unknown>;
      if (typeof type === "string" && TEXT_BLOCK_TYPES.has(type) && typeof text === "string") {
        parts.push(text);
      }
    }
  }
  const text = parts.join("").trim();
  return text ? { role: record.role, text } : undefined;
}

/** The user and agent messages among raw session items, in order. */
export function transcriptMessages(items: readonly unknown[]): TranscriptMessage[] {
  return items.flatMap((item) => {
    const message = transcriptMessage(item);
    return message ? [message] : [];
  });
}

/**
 * Cuts `text` to `EXCERPT_CHARS` around `anchor`, else its first range, shifting the ranges
 * to match.
 */
function clip(
  text: string,
  ranges: readonly MatchRange[],
  anchor: MatchRange | undefined,
): Omit<PreviewMessage, "role" | "matched" | "position"> {
  if (text.length <= EXCERPT_CHARS) {
    return { text, ranges, clippedStart: false, clippedEnd: false };
  }
  const first = (anchor ?? ranges[0])?.start ?? 0;
  const start = Math.max(0, Math.min(first - EXCERPT_CHARS / 4, text.length - EXCERPT_CHARS));
  const end = start + EXCERPT_CHARS;
  return {
    text: text.slice(start, end),
    ranges: ranges
      .filter((range) => range.start >= start && range.end <= end)
      .map((range) => ({ start: range.start - start, end: range.end - start })),
    clippedStart: start > 0,
    clippedEnd: end < text.length,
  };
}

/** The messages from `from` up to `to`, each marked as the match or not. */
function previewMessages(
  messages: readonly TranscriptMessage[],
  {
    from,
    to,
    query,
    matchIndex,
    phrase,
  }: {
    from: number;
    to: number;
    query: string;
    matchIndex: number;
    /** Where the whole query sits in the matching message. */
    phrase: MatchRange | undefined;
  },
): PreviewMessage[] {
  return messages.slice(from, to).map((message, offset) => {
    const position = from + offset;
    const ranges = query ? occurrenceRanges(query, message.text) : [];
    return {
      role: message.role,
      matched: position === matchIndex,
      position,
      ...clip(message.text, ranges, position === matchIndex ? phrase : undefined),
    };
  });
}

/**
 * Where `text` first holds `query` as one case- and accent-folded phrase, as the server
 * matches it, in `text`'s own offsets.
 */
function phraseRange(query: string, text: string): MatchRange | undefined {
  const phrase = foldText(query).values.join("");
  const target = foldText(text);
  // Folding can change a character's length, so map the joined string back to characters.
  const characterAt: number[] = [];
  target.values.forEach((value, index) => {
    for (let offset = 0; offset < value.length; offset += 1) characterAt.push(index);
  });
  const found = target.values.join("").indexOf(phrase);
  if (!phrase || found === -1) return undefined;
  const first = target.characters[characterAt[found]];
  const last = target.characters[characterAt[found + phrase.length - 1]];
  return { start: first.sourceOffset, end: last.sourceOffset + last.sourceLength };
}

/**
 * The newest message that holds `query`, with one message either side; without one, the
 * latest messages. An empty query previews the latest messages, as a recent session would.
 */
export function sessionPreviewExcerpt(
  messages: readonly TranscriptMessage[],
  query: string,
): SessionPreviewExcerpt {
  const trimmed = query.trim();
  let matchIndex = -1;
  let phrase: MatchRange | undefined;
  if (trimmed) {
    // The server matches the whole query as one phrase, so the preview does too.
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      phrase = phraseRange(trimmed, messages[index].text);
      if (phrase) {
        matchIndex = index;
        break;
      }
    }
  }
  if (matchIndex === -1) {
    const from = Math.max(0, messages.length - LATEST_MESSAGES);
    return {
      messages: previewMessages(messages, {
        from,
        to: messages.length,
        query: trimmed,
        matchIndex,
        phrase,
      }),
      found: false,
    };
  }
  const from = Math.max(0, matchIndex - CONTEXT_MESSAGES);
  const to = Math.min(messages.length, matchIndex + CONTEXT_MESSAGES + 1);
  return {
    messages: previewMessages(messages, { from, to, query: trimmed, matchIndex, phrase }),
    found: true,
  };
}

/**
 * What the preview shows for one page of a session's latest items:
 * - `messages`: the excerpt, with the search's snippet when the match is older than the page.
 * - `older-only`: the page holds no messages, such as only tool calls, but history goes further
 *   back; the snippet still says where the query matched.
 * - `empty`: the session has no messages at all.
 */
export type SessionPreviewState =
  | { kind: "messages"; excerpt: SessionPreviewExcerpt; snippet: string | undefined }
  | { kind: "older-only"; snippet: string | undefined }
  | { kind: "empty" };

export function sessionPreviewState(
  page: { items: readonly unknown[]; hasMore: boolean },
  query: string,
  searchSnippet: string | null | undefined,
): SessionPreviewState {
  const excerpt = sessionPreviewExcerpt(transcriptMessages(page.items), query);
  // The snippet stands in for a match the page doesn't reach; without a query there is none.
  const snippet = !excerpt.found && query.trim() && searchSnippet ? searchSnippet : undefined;
  if (excerpt.messages.length > 0) return { kind: "messages", excerpt, snippet };
  return page.hasMore ? { kind: "older-only", snippet } : { kind: "empty" };
}
