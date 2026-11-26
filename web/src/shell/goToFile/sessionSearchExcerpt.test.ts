import { describe, expect, it } from "vitest";

import {
  EXCERPT_CHARS,
  sessionPreviewExcerpt,
  sessionPreviewState,
  transcriptMessage,
  transcriptMessages,
  type TranscriptMessage,
} from "./sessionSearchExcerpt";

const user = (text: string): TranscriptMessage => ({ role: "user", text });
const agent = (text: string): TranscriptMessage => ({ role: "assistant", text });

describe("transcriptMessage", () => {
  it("reads a user's input text and an agent's output text", () => {
    expect(
      transcriptMessage({
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "Fix the upload" }],
      }),
    ).toEqual({ role: "user", text: "Fix the upload" });
    expect(
      transcriptMessage({
        type: "message",
        role: "assistant",
        content: [
          { type: "output_text", text: "Added a retry " },
          { type: "output_text", text: "backoff." },
        ],
      }),
    ).toEqual({ role: "assistant", text: "Added a retry backoff." });
  });

  it("skips tool calls, system messages, empty text and other shapes", () => {
    expect(transcriptMessage({ type: "function_call", name: "shell" })).toBeUndefined();
    expect(
      transcriptMessage({
        type: "message",
        role: "system",
        content: [{ type: "text", text: "x" }],
      }),
    ).toBeUndefined();
    expect(
      transcriptMessage({
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "  " }],
      }),
    ).toBeUndefined();
    expect(transcriptMessage(null)).toBeUndefined();
    expect(transcriptMessage("text")).toBeUndefined();
  });

  it("keeps only the messages, in order", () => {
    expect(
      transcriptMessages([
        { type: "message", role: "user", content: "first" },
        { type: "function_call", name: "shell" },
        { type: "message", role: "assistant", content: [{ type: "output_text", text: "second" }] },
      ]),
    ).toEqual([user("first"), agent("second")]);
  });
});

describe("sessionPreviewExcerpt", () => {
  const messages = [
    user("uploads time out on slow links"),
    agent("I'll wrap the call in a retry backoff with jitter"),
    user("keep the cap at 30s"),
    agent("Done, the cap is 30s"),
  ];

  it("shows the newest message with the query and one message either side, marking the match", () => {
    const excerpt = sessionPreviewExcerpt(messages, "backoff");
    expect(excerpt.found).toBe(true);
    expect(excerpt.messages.map(({ text, matched }) => [text, matched])).toEqual([
      [messages[0].text, false],
      [messages[1].text, true],
      [messages[2].text, false],
    ]);
    const hit = excerpt.messages[1];
    expect(hit.ranges.map(({ start, end }) => hit.text.slice(start, end))).toEqual(["backoff"]);
  });

  it("prefers the newest of several matching messages", () => {
    const excerpt = sessionPreviewExcerpt(messages, "cap");
    expect(excerpt.messages.find((message) => message.matched)?.text).toBe("Done, the cap is 30s");
    // The last message has nothing after it.
    expect(excerpt.messages).toHaveLength(2);
  });

  it("marks the message holding the whole phrase, as the server matched it, over a newer one with only its words", () => {
    const excerpt = sessionPreviewExcerpt(
      [
        user("Wrap the upload in a retry backoff so slow links stop"),
        agent("Added the retry backoff."),
      ],
      "Retry Backoff so slow links",
    );
    expect(excerpt.found).toBe(true);
    expect(excerpt.messages.find((message) => message.matched)?.text).toBe(
      "Wrap the upload in a retry backoff so slow links stop",
    );
  });

  it("falls back to the latest messages when none holds the query, or there is no query", () => {
    for (const query of ["kubernetes", "", "   "]) {
      const excerpt = sessionPreviewExcerpt(messages, query);
      expect(excerpt.found).toBe(false);
      expect(excerpt.messages.map((message) => message.text)).toEqual(
        messages.slice(-3).map((message) => message.text),
      );
      expect(excerpt.messages.every((message) => !message.matched)).toBe(true);
    }
  });

  it("cuts a long message to the part around its match, shifting the ranges with it", () => {
    const long = `${"a".repeat(2_000)} needle ${"b".repeat(2_000)}`;
    const [hit] = sessionPreviewExcerpt([agent(long)], "needle").messages;
    expect(hit.text.length).toBe(EXCERPT_CHARS);
    expect(hit.clippedStart).toBe(true);
    expect(hit.clippedEnd).toBe(true);
    expect(hit.ranges.map(({ start, end }) => hit.text.slice(start, end))).toEqual(["needle"]);
  });

  it("cuts a long message around the whole phrase, not an earlier lone word from it", () => {
    const phrase = "retry backoff so slow links";
    const long = `retry ${"a".repeat(2_000)} ${phrase} ${"b".repeat(2_000)}`;
    const [hit] = sessionPreviewExcerpt([agent(long)], phrase).messages;
    expect(hit.matched).toBe(true);
    expect(hit.text.length).toBe(EXCERPT_CHARS);
    expect(hit.text).toContain(phrase);

    // Folding keeps the source offsets: a decomposed accent before it, the query in other case.
    const accented = `café ${"a".repeat(2_000)} ${phrase} ${"b".repeat(2_000)}`;
    const [folded] = sessionPreviewExcerpt(
      [agent(accented)],
      "Retry Backoff SO slow links",
    ).messages;
    expect(folded.text).toContain(phrase);
  });

  it("keeps a long message's start when the match is near it", () => {
    const [hit] = sessionPreviewExcerpt([agent(`needle ${"b".repeat(2_000)}`)], "needle").messages;
    expect(hit.clippedStart).toBe(false);
    expect(hit.clippedEnd).toBe(true);
    expect(hit.text.startsWith("needle")).toBe(true);
  });

  it("previews nothing for a session with no messages", () => {
    expect(sessionPreviewExcerpt([], "anything")).toEqual({ messages: [], found: false });
  });
});

describe("sessionPreviewState", () => {
  const toolCalls = Array.from({ length: 50 }, (_, index) => ({
    type: "function_call",
    name: "shell",
    call_id: `call_${index}`,
  }));
  const snippet = "…wrap the call in a retry backoff…";
  const messageItem = (text: string) => ({ type: "message", role: "user", content: text });

  it("keeps the search's snippet when the latest page holds only tool calls and history goes back further", () => {
    expect(sessionPreviewState({ items: toolCalls, hasMore: true }, "backoff", snippet)).toEqual({
      kind: "older-only",
      snippet,
    });
  });

  it("says the session is empty only when no history is left to read", () => {
    expect(sessionPreviewState({ items: [], hasMore: false }, "backoff", snippet)).toEqual({
      kind: "empty",
    });
    expect(sessionPreviewState({ items: toolCalls, hasMore: false }, "", undefined)).toEqual({
      kind: "empty",
    });
  });

  it("has no snippet to show without a query", () => {
    expect(sessionPreviewState({ items: toolCalls, hasMore: true }, "  ", snippet)).toEqual({
      kind: "older-only",
      snippet: undefined,
    });
  });

  it("shows the snippet beside the latest messages when the match is older than them", () => {
    const state = sessionPreviewState(
      { items: [messageItem("keep the cap at 30s")], hasMore: true },
      "backoff",
      snippet,
    );
    expect(state).toMatchObject({ kind: "messages", snippet, excerpt: { found: false } });
  });

  it("leaves the snippet out when a loaded message holds the match", () => {
    const state = sessionPreviewState(
      { items: [messageItem("add a retry backoff")], hasMore: true },
      "backoff",
      snippet,
    );
    expect(state).toMatchObject({ kind: "messages", snippet: undefined, excerpt: { found: true } });
  });
});
