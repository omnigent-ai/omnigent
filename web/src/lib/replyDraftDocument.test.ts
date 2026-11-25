import { describe, expect, it } from "vitest";
import {
  appendQuoteToDocument,
  composerTextRegions,
  mapComposerSelection,
  minimalTextChange,
  parseReplyQuoteLine,
  quoteRemovalSpan,
  removeReplyQuoteAt,
  removeReplyQuoteLine,
  replaceTail,
  replyQuoteClipboardText,
  replyQuoteLine,
  REPLY_QUOTE_END,
  REPLY_QUOTE_MARK,
  sameReplyDraft,
  tailOf,
  typedDocumentFor,
  unweaveReplyDraft,
  weaveReplyDraft,
  withoutReplyQuoteMarks,
} from "./replyDraftDocument";
import type { ReplyDraft } from "./replyDraft";

const quote = (id: string, text: string, before = "") => ({ id, before, text });

describe("reply draft document", () => {
  it("round-trips a draft through its document", () => {
    const draft: ReplyDraft = {
      quotes: [quote("a", "First\nline", "Intro"), quote("b", "Second", "")],
      text: "Reply",
    };
    const document = weaveReplyDraft(draft);
    expect(unweaveReplyDraft(document)).toEqual(draft);
    expect(weaveReplyDraft(unweaveReplyDraft(document))).toBe(document);
  });

  it("opens on the first card when its text before is empty", () => {
    const document = weaveReplyDraft({ quotes: [quote("a", "Q")], text: "" });
    expect(document.split("\n")[0]).toBe(replyQuoteLine({ id: "a", text: "Q" }));
    expect(document.split("\n")).toHaveLength(2);
  });

  it("keeps an empty line before a later card, as the textarea keeps its empty input", () => {
    const document = weaveReplyDraft({
      quotes: [quote("a", "One"), quote("b", "Two")],
      text: "",
    });
    expect(document.split("\n")[1]).toBe("");
  });

  it("strips marks from plain text, so typed text can't pose as a card", () => {
    const token = replyQuoteLine({ id: "x", text: "Pasted" });
    const document = weaveReplyDraft({
      quotes: [quote("a", "Real", `before ${token} after`)],
      text: `tail${token}`,
    });
    expect(unweaveReplyDraft(document)).toEqual({
      quotes: [quote("a", "Real", "before  after")],
      text: "tail",
    });
    expect(withoutReplyQuoteMarks(`a${token}c`)).toBe("ac");
    expect(withoutReplyQuoteMarks(`a${REPLY_QUOTE_MARK}!${REPLY_QUOTE_END}c`)).toBe("a!c");
  });

  it("strips a stray mark from plain text, and re-weaves a typed document that holds one", () => {
    const draft: ReplyDraft = { quotes: [quote("a", "Q")], text: "tail" };
    expect(
      unweaveReplyDraft(`Head${REPLY_QUOTE_MARK}\n${replyQuoteLine({ id: "a", text: "Q" })}\ntail`)
        .quotes[0]!.before,
    ).toBe("Head");
    const typed = `Head${REPLY_QUOTE_MARK}\n${replyQuoteLine({ id: "a", text: "Q" })}\ntail`;
    expect(typedDocumentFor(draft, typed)).toBe(weaveReplyDraft(draft));
  });

  it("gives a repeated token id a fresh suffix, the same way every time", () => {
    const token = replyQuoteLine({ id: "dup", text: "Copy" });
    const document = [token, token].join("\n");
    const first = unweaveReplyDraft(document);
    expect(first.quotes.map((item) => item.id)).toEqual(["dup", "dup-2"]);
    expect(unweaveReplyDraft(document)).toEqual(first);
  });

  it("parses only well-formed token lines", () => {
    expect(parseReplyQuoteLine("plain")).toBeNull();
    expect(parseReplyQuoteLine(`${REPLY_QUOTE_MARK}!!${REPLY_QUOTE_END}`)).toBeNull();
    expect(parseReplyQuoteLine(`${REPLY_QUOTE_MARK}${REPLY_QUOTE_END}`)).toBeNull();
  });

  it("keeps a typed document while it still describes the draft", () => {
    // A blank line the user typed above the card belongs to the draft's text before it.
    const typed = `Head\n\n${replyQuoteLine({ id: "a", text: "Q" })}\nTail`;
    const draft = unweaveReplyDraft(typed);
    expect(typedDocumentFor(draft, typed)).toBe(typed);
    expect(typedDocumentFor(draft, null)).toBe(weaveReplyDraft(draft));
    const changed = { ...draft, text: "Other" };
    expect(typedDocumentFor(changed, typed)).toBe(weaveReplyDraft(changed));
  });

  it("compares drafts by quotes, texts and ids", () => {
    const draft: ReplyDraft = { quotes: [quote("a", "Q")], text: "" };
    expect(sameReplyDraft(draft, { quotes: [quote("a", "Q")], text: "" })).toBe(true);
    expect(sameReplyDraft(draft, { quotes: [quote("b", "Q")], text: "" })).toBe(false);
  });

  it("appends a card after the document's text, with an empty line under it", () => {
    expect(appendQuoteToDocument("", { id: "n", text: "New" })).toBe(
      `${replyQuoteLine({ id: "n", text: "New" })}\n`,
    );
    expect(appendQuoteToDocument("Opener", { id: "n", text: "New" })).toBe(
      `Opener\n${replyQuoteLine({ id: "n", text: "New" })}\n`,
    );
  });

  it("places the tail of a document that ends in a card at its end", () => {
    const card = replyQuoteLine({ id: "a", text: "A" });
    const regions = composerTextRegions(card, ["a"], "tail");
    expect(regions.at(-1)).toEqual({ inputId: "tail", start: card.length, end: card.length });
  });

  it("uses the noncharacter marks, so the private-use area is plain text", () => {
    expect(REPLY_QUOTE_MARK).toBe("\uFDD0");
    expect(REPLY_QUOTE_END).toBe("\uFDD1");
    expect(withoutReplyQuoteMarks("\uE000\uE001 icon")).toBe("\uE000\uE001 icon");
  });

  it("replaces only the tail, so the cards and the text before them stay", () => {
    const card = replyQuoteLine({ id: "a", text: "A" });
    const document = `Head\n${card}\nTail`;
    expect(tailOf(document)).toBe("Tail");
    expect(replaceTail(document, "")).toBe(`Head\n${card}\n`);
    expect(replaceTail(document, "/side ")).toBe(`Head\n${card}\n/side `);
    // A card always has a line after it, even when the tail is empty.
    expect(replaceTail(card, "New")).toBe(`${card}\nNew`);
    // With no card, the whole document is the tail.
    expect(replaceTail("Plain", "")).toBe("");
  });

  it("strips marks from a tail written in code, so it can't add a card", () => {
    const card = replyQuoteLine({ id: "a", text: "A" });
    expect(replaceTail(`Head\n${card}\n`, `x${card}y`)).toBe(`Head\n${card}\nxy`);
  });

  it("finds the one change between two documents", () => {
    expect(minimalTextChange("abc", "abc")).toBeNull();
    expect(minimalTextChange("abXc", "abYZc")).toEqual({ from: 2, to: 3, insert: "YZ" });
    expect(minimalTextChange("", "new")).toEqual({ from: 0, to: 0, insert: "new" });
  });

  it("never starts or ends a change inside a surrogate pair", () => {
    // 😀 and 😁 share a high surrogate, so a bare code-unit diff would start inside the pair.
    expect(minimalTextChange("a😀b", "a😁b")).toEqual({ from: 1, to: 3, insert: "😁" });
    // Here the low surrogates match, so a bare diff would end inside the pair.
    expect(minimalTextChange("😀😀", "😁😀")).toEqual({ from: 0, to: 2, insert: "😁" });
  });

  it("copies cards as markdown quotes", () => {
    const text = `Head\n${replyQuoteLine({ id: "a", text: "one\ntwo" })}\nTail`;
    expect(replyQuoteClipboardText(text)).toBe("Head\n> one\n> two\nTail");
  });
});

describe("card removal", () => {
  const card = replyQuoteLine({ id: "c", text: "Card" });

  it("removes the card's line and one break, keeping the lines around it", () => {
    expect(removeReplyQuoteLine(`Opener\n${card}\nWhy`, 1)).toBe("Opener\nWhy");
  });

  it("removes the last line without leaving a trailing break", () => {
    expect(removeReplyQuoteLine(`Opener\n${card}`, 1)).toBe("Opener");
  });

  it("removes a blank line that separates the card from the document's start", () => {
    expect(removeReplyQuoteLine(`\n${card}\nRest`, 1)).toBe("Rest");
  });

  it("removes the first card's blank line below it, so no blank line is left at the top", () => {
    const other = replyQuoteLine({ id: "o", text: "Other" });
    expect(removeReplyQuoteLine(`${card}\n\n${other}\nRest`, 0)).toBe(`${other}\nRest`);
    expect(quoteRemovalSpan([card, "", "Rest"], 0)).toEqual({ first: 0, last: 0 });
  });

  it("removes the blank line that separates the card from the card above", () => {
    const other = replyQuoteLine({ id: "o", text: "Other" });
    expect(removeReplyQuoteLine(`${other}\n\n${card}\nRest`, 2)).toBe(`${other}\nRest`);
  });

  it("keeps a blank line after the card, which belongs to the text", () => {
    expect(removeReplyQuoteLine(`${card}\n\nRest`, 0)).toBe("\nRest");
  });

  it("keeps a blank line above the card when it follows text", () => {
    expect(quoteRemovalSpan(["Opener", "", card, "Rest"], 2)).toEqual({ first: 2, last: 2 });
  });

  it("removes a document that ends right in a card", () => {
    expect(removeReplyQuoteLine(card, 0)).toBe("");
  });

  it("removes the card at a position in the draft, not by its id", () => {
    const other = replyQuoteLine({ id: "dup", text: "Other" });
    const copy = replyQuoteLine({ id: "dup", text: "Copy" });
    expect(removeReplyQuoteAt(`${other}\n${copy}\nRest`, 1)).toBe(`${other}\nRest`);
  });

  it("leaves a plain line alone", () => {
    expect(removeReplyQuoteLine("Plain\nText", 0)).toBe("Plain\nText");
  });
});

describe("composer text regions", () => {
  it("lists each card's text before it, then the tail", () => {
    const document = `Head\n${replyQuoteLine({ id: "a", text: "A" })}\nMid\n${replyQuoteLine({
      id: "b",
      text: "B",
    })}\nTail`;
    const regions = composerTextRegions(document, ["a", "b"], "tail");
    expect(regions.map((region) => region.inputId)).toEqual(["a", "b", "tail"]);
    expect(document.slice(regions[0]!.start, regions[0]!.end)).toBe("Head");
    expect(document.slice(regions[1]!.start, regions[1]!.end)).toBe("Mid");
    expect(document.slice(regions[2]!.start, regions[2]!.end)).toBe("Tail");
  });
});

describe("caret mapping across engines", () => {
  const card = replyQuoteLine({ id: "a", text: "A" });
  const document = `Head\n${card}\nTail`;
  const ids = ["a"];

  it("maps a textarea caret in a card's text before it to the document", () => {
    expect(
      mapComposerSelection(
        document,
        { inputId: "a", selectionStart: 2, selectionEnd: 2 },
        "textarea",
        ids,
        "tail",
      ),
    ).toEqual({ inputId: "tail", selectionStart: 2, selectionEnd: 2 });
  });

  it("maps a document caret in the tail to the tail's offset", () => {
    const at = document.indexOf("Tail") + 2;
    expect(
      mapComposerSelection(
        document,
        { inputId: "tail", selectionStart: at, selectionEnd: at },
        "codemirror",
        ids,
        "tail",
      ),
    ).toEqual({ inputId: "tail", selectionStart: 2, selectionEnd: 2 });
  });

  it("maps a document caret in a card's text to that card's input", () => {
    expect(
      mapComposerSelection(
        document,
        { inputId: "tail", selectionStart: 3, selectionEnd: 3 },
        "codemirror",
        ids,
        "tail",
      ),
    ).toEqual({ inputId: "a", selectionStart: 3, selectionEnd: 3 });
  });

  it("collapses a selection that ends in another input to its start", () => {
    expect(
      mapComposerSelection(
        document,
        { inputId: "tail", selectionStart: 1, selectionEnd: document.length },
        "codemirror",
        ids,
        "tail",
      ),
    ).toEqual({ inputId: "a", selectionStart: 1, selectionEnd: 1 });
  });

  it("maps a document-start caret before a leading card to the tail, not to its hidden input", () => {
    const leading = replyQuoteLine({ id: "a", text: "A" });
    expect(
      mapComposerSelection(
        `${leading}\nTail`,
        { inputId: "tail", selectionStart: 0, selectionEnd: 0 },
        "codemirror",
        ["a"],
        "tail",
      ),
    ).toEqual({ inputId: "tail", selectionStart: 0, selectionEnd: 0 });
  });

  it("puts a caret from a card that is gone at the tail's end", () => {
    expect(
      mapComposerSelection(
        "Tail",
        { inputId: "gone", selectionStart: 1, selectionEnd: 1 },
        "textarea",
        [],
        "tail",
      ),
    ).toEqual({ inputId: "tail", selectionStart: 4, selectionEnd: 4 });
  });
});
