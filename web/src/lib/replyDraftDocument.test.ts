import { describe, expect, it } from "vitest";
import type { ReplyDraft } from "./replyDraft";
import {
  appendQuoteToDocument,
  parseQuoteLine,
  quoteLine,
  quoteRemovalRange,
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
      text: "xyz\nbad\ntail",
    });
    expect(parseQuoteLine("\uFDD0bm90IGpzb24\uFDD1")).toBeNull();
  });

  it("round-trips any text, its token using base64url's - and _", () => {
    const text = "héllo 👋 日本語 ~~~???";
    const line = quoteLine("id-1", text);
    expect(line).toMatch(/-/);
    expect(line).toMatch(/_/);
    expect(parseQuoteLine(line)).toEqual({ id: "id-1", text });
  });

  it.each([
    ["three fields", ["a", "b", "c"]],
    ["a numeric id", [1, "b"]],
    ["non-string text", ["a", null]],
  ])("rejects a token with %s", (_name, payload) => {
    const encoded = btoa(JSON.stringify(payload)).replace(/=+$/, "");
    expect(
      parseQuoteLine(`\uFDD0${encoded.replace(/\+/g, "-").replace(/\//g, "_")}\uFDD1`),
    ).toBeNull();
  });

  it("weaves adjacent cards back with the blank line the textarea shows as an empty input", () => {
    expect(weaveReplyDraft(unweaveReplyDraft(doc(a, b, "")))).toBe(doc(a, "", b, ""));
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

  it("removes a card by its line's offsets, and leaves an unknown quote alone", () => {
    expect(quoteRemovalRange(doc("X", a, "Y"), "a")).toEqual({ from: 2, to: 2 + a.length + 1 });
    expect(quoteRemovalRange(doc("X", a, ""), "a")).toEqual({ from: 1, to: 2 + a.length + 1 });
    expect(quoteRemovalRange(doc("X", a, ""), "missing")).toBeNull();
    expect(removeQuoteFromDocument(doc("X", a, ""), "missing")).toBe(doc("X", a, ""));
  });

  it("replaces only the tail, keeping every card", () => {
    expect(replaceQuoteTail(doc("X", a, "old", "lines"), `new${b}`)).toBe(doc("X", a, "new"));
    expect(replaceQuoteTail("plain", "next")).toBe("next");
    expect(replaceQuoteTail(doc("X", a, ""), "one\r\ntwo\rthree")).toBe(
      doc("X", a, "one", "two", "three"),
    );
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

  it("keeps a damaged token as text without its marks, so a tail write writes it once", () => {
    const damaged = "\uFDD0bad\uFDD1";
    expect(settleQuoteLines(doc(`X${damaged}${a}${damaged}`, "\uFDD1"))).toBe(
      doc("Xbad", a, "bad", ""),
    );
    const document = doc("X", a, "Y", damaged, "Z");
    const tail = unweaveReplyDraft(document).text;
    expect(tail).toBe(doc("Y", "bad", "Z"));
    expect(replaceQuoteTail(document, `${tail}!`)).toBe(doc("X", a, "Y", "bad", "Z!"));
  });

  it("finds where the text starts after the cards that open a document", () => {
    expect(quoteTextStart(doc(a, b, "Text"))).toBe(doc(a, b, "").length);
    expect(quoteTextStart(doc("Text", a, ""))).toBe(0);
    expect(quoteTextStart(doc(a, b))).toBe(doc(a, b).length);
  });

  it("copies quotes as markdown", () => {
    expect(quotesAsMarkdown(doc("X", b, "Y"))).toBe(doc("X", "> Beta", "> more", "Y"));
  });
});
