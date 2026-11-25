import { describe, expect, it } from "vitest";
import {
  hasReplyQuoteLines,
  parseReplyQuoteLine,
  replyQuoteClipboardText,
  REPLY_QUOTE_MARK,
  unweaveReplyDraft,
  weaveReplyDraft,
  withoutReplyQuoteMarks,
} from "./replyDraftDocument";
import { serializeReplyDraft, type ReplyDraft } from "./replyDraft";

const twoQuotes: ReplyDraft = {
  quotes: [
    { id: "first", before: "Introduction", text: "First line\n\nNext paragraph" },
    { id: "second", before: "First answer", text: "Second quote" },
  ],
  text: "Second answer",
};

describe("reply draft documents", () => {
  it("is the plain text when there are no quotes", () => {
    expect(weaveReplyDraft({ quotes: [], text: "Hello\nthere" })).toBe("Hello\nthere");
    expect(unweaveReplyDraft("Hello\nthere")).toEqual({ quotes: [], text: "Hello\nthere" });
  });

  it("puts each quote on its own line between the text around it", () => {
    const document = weaveReplyDraft(twoQuotes);
    const lines = document.split("\n");
    expect(lines.map((line) => line.startsWith(REPLY_QUOTE_MARK))).toEqual([
      false,
      true,
      false,
      true,
      false,
    ]);
    expect(lines[0]).toBe("Introduction");
    expect(lines[2]).toBe("First answer");
    expect(lines[4]).toBe("Second answer");
  });

  it("round-trips quotes with ids, multiline text and blank lines", () => {
    expect(unweaveReplyDraft(weaveReplyDraft(twoQuotes))).toEqual(twoQuotes);
  });

  it("round-trips a quote at the start with an empty `before`", () => {
    const draft: ReplyDraft = { quotes: [{ id: "a", before: "", text: "Only" }], text: "reply" };
    expect(unweaveReplyDraft(weaveReplyDraft(draft))).toEqual(draft);
  });

  it("round-trips a quote at the end with empty trailing text", () => {
    const draft: ReplyDraft = { quotes: [{ id: "a", before: "Hi", text: "Quoted" }], text: "" };
    expect(unweaveReplyDraft(weaveReplyDraft(draft))).toEqual(draft);
  });

  it("round-trips several quotes with empty `before` between them", () => {
    const draft: ReplyDraft = {
      quotes: [
        { id: "a", before: "", text: "One" },
        { id: "b", before: "", text: "Two" },
      ],
      text: "",
    };
    expect(unweaveReplyDraft(weaveReplyDraft(draft))).toEqual(draft);
  });

  it("keeps any text exactly, including the characters of a quote's markdown", () => {
    const text = "> not a quote\n# heading\n😀 café ​\\ \" '";
    const draft: ReplyDraft = { quotes: [{ id: "x", before: "> typed", text }], text: "> also" };
    expect(unweaveReplyDraft(weaveReplyDraft(draft))).toEqual(draft);
  });

  it("removing a quote's line leaves the other quotes and the text they sit between", () => {
    const lines = weaveReplyDraft(twoQuotes).split("\n");
    const withoutFirst = lines.filter((line) => line !== lines[1]).join("\n");
    expect(unweaveReplyDraft(withoutFirst)).toEqual({
      quotes: [{ id: "second", before: "Introduction\nFirst answer", text: "Second quote" }],
      text: "Second answer",
    });
  });

  it("reads a document whose blank line was deleted as the quotes it still holds", () => {
    const draft: ReplyDraft = {
      quotes: [
        { id: "a", before: "", text: "One" },
        { id: "b", before: "", text: "Two" },
      ],
      text: "",
    };
    const lines = weaveReplyDraft(draft).split("\n");
    const document = lines.filter((_, index) => index !== 2).join("\n");
    expect(unweaveReplyDraft(document)).toEqual({
      quotes: [
        { id: "a", before: "", text: "One" },
        { id: "b", before: "", text: "Two" },
      ],
      text: "",
    });
  });

  it("serializes the same markdown from the document's draft", () => {
    const draft = unweaveReplyDraft(weaveReplyDraft(twoQuotes));
    expect(serializeReplyDraft(draft)).toBe(serializeReplyDraft(twoQuotes));
  });

  it("treats a damaged quote line as plain text", () => {
    const damaged = `${REPLY_QUOTE_MARK}not-base64!`;
    expect(parseReplyQuoteLine(damaged)).toBeNull();
    expect(unweaveReplyDraft(`top\n${damaged}`)).toEqual({
      quotes: [],
      text: `top\n${damaged}`,
    });
  });

  it("rejects a payload that is valid base64 but not an id and text pair", () => {
    const line = REPLY_QUOTE_MARK + btoa('{"id":"x"}').replace(/=+$/, "");
    expect(parseReplyQuoteLine(line)).toBeNull();
  });

  it("reports whether a document holds a quote line", () => {
    expect(hasReplyQuoteLines(weaveReplyDraft(twoQuotes))).toBe(true);
    expect(hasReplyQuoteLines("plain\ntext")).toBe(false);
  });

  it("strips quote marks from pasted text", () => {
    expect(withoutReplyQuoteMarks(`a${REPLY_QUOTE_MARK}b`)).toBe("ab");
  });

  it("copies quote lines as markdown quotes and leaves other lines as they are", () => {
    const document = weaveReplyDraft(twoQuotes);
    expect(replyQuoteClipboardText(document)).toBe(
      "Introduction\n> First line\n> \n> Next paragraph\nFirst answer\n> Second quote\nSecond answer",
    );
  });
});
