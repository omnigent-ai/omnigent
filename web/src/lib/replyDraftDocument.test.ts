import { describe, expect, it } from "vitest";
import type { ReplyDraft } from "./replyDraft";
import {
  appendQuoteToDocument,
  parseQuoteLine,
  quoteLine,
  quotesAsMarkdown,
  quoteTextStart,
  removeQuoteFromDocument,
  replaceQuoteTail,
  settleQuoteLines,
  unweaveReplyDraft,
  weaveReplyDraft,
} from "./replyDraftDocument";

const a = quoteLine("a", "Alpha");
const b = quoteLine("b", "Beta\nmore");
const doc = (...lines: string[]) => lines.join("\n");

describe("weaveReplyDraft and unweaveReplyDraft", () => {
  it.each<[string, ReplyDraft, string]>([
    ["no quotes", { quotes: [], text: "Just text" }, "Just text"],
    [
      "text before a quote",
      { quotes: [{ id: "a", before: "Intro\n\nmore", text: "Alpha" }], text: "Answer" },
      doc("Intro", "", "more", a, "Answer"),
    ],
    [
      "a quote that opens the draft, then one after an empty input",
      {
        quotes: [
          { id: "a", before: "", text: "Alpha" },
          { id: "b", before: "", text: "Beta\nmore" },
        ],
        text: "",
      },
      doc(a, "", b, ""),
    ],
  ])("round-trips %s", (_name, draft, document) => {
    expect(weaveReplyDraft(draft)).toBe(document);
    expect(unweaveReplyDraft(document)).toEqual(draft);
  });

  it("makes no card from marks in plain text", () => {
    const pasted = `x${a}y\uFDD0z`;
    expect(weaveReplyDraft({ quotes: [], text: pasted })).toBe("xyz");
    expect(unweaveReplyDraft(doc(pasted, "\uFDD0bad\uFDD1", "tail"))).toEqual({
      quotes: [],
      text: "xyz\n\ntail",
    });
    expect(parseQuoteLine("\uFDD0bm90IGpzb24\uFDD1")).toBeNull();
  });
});

describe("document edits", () => {
  it.each([
    ["text on both sides", doc("X", a, "Y"), "a", doc("X", "Y")],
    ["the last card with its tail line", doc("X", a, ""), "a", "X"],
    ["the only card", doc(a, ""), "a", ""],
    ["a card with the blank line above it", doc(a, "", b, "Y"), "b", doc(a, "Y")],
    ["a first card with the blank line below it", doc(a, "", b, "Y"), "a", doc(b, "Y")],
    ["a card after text, with its separator", doc("O", a, "", b, ""), "a", doc("O", b, "")],
    ["the card under it", doc("O", a, "", b, ""), "b", doc("O", a, "")],
    ["adjacent cards, keeping the tail line", doc(a, b, ""), "b", doc(a, "")],
    ["a blank line that is text", doc("X", "", a, "Y"), "a", doc("X", "", "Y")],
  ])("removes %s", (_name, document, id, expected) => {
    expect(removeQuoteFromDocument(document, id)).toBe(expected);
  });

  it("replaces only the tail, keeping every card", () => {
    expect(replaceQuoteTail(doc("X", a, "old", "lines"), `new${b}`)).toBe(doc("X", a, "new"));
    expect(replaceQuoteTail("plain", "next")).toBe("next");
  });

  it("appends a quote after the tail and ends on an empty tail line", () => {
    const appended = appendQuoteToDocument(doc("X", a, "Y"), "New\r\nquote");
    expect(unweaveReplyDraft(appended)).toMatchObject({
      quotes: [{ before: "X" }, { before: "Y", text: "New\nquote" }],
      text: "",
    });
    expect(appended.endsWith("\n")).toBe(true);
    expect(unweaveReplyDraft(appendQuoteToDocument("", "First")).quotes[0]!.before).toBe("");
  });

  it("puts each token on its own line and keeps a tail line", () => {
    expect(settleQuoteLines(`before${a}after`)).toBe(doc("before", a, "after"));
    expect(settleQuoteLines(`${a}${b}`)).toBe(doc(a, b, ""));
  });

  it("finds where the text starts after the cards that open a document", () => {
    expect(quoteTextStart(doc(a, b, "Text"))).toBe(doc(a, b, "").length);
    expect(quoteTextStart(doc("Text", a, ""))).toBe(0);
  });

  it("copies quotes as markdown", () => {
    expect(quotesAsMarkdown(doc("X", b, "Y"))).toBe(doc("X", "> Beta", "> more", "Y"));
  });
});
