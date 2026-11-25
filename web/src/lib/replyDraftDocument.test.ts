import { describe, expect, it } from "vitest";
import {
  appendQuoteToDocument,
  composerTextRegions,
  mapComposerSelection,
  parseReplyQuoteLine,
  quoteRemovalSpan,
  removeReplyQuoteAt,
  settleReplyDocument,
  removeReplyQuoteLine,
  replyQuoteClipboardText,
  replyQuoteLine,
  REPLY_QUOTE_END,
  REPLY_QUOTE_MARK,
  sameReplyDraft,
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

  it("settles an edit made in code the way the editor settles its own", () => {
    const card = replyQuoteLine({ id: "a", text: "A" });
    // Text typed beside a card goes on its own line, and a card never ends the document.
    expect(settleReplyDocument(`Head${card}Tail`, card)).toBe(`Head\n${card}\nTail`);
    expect(settleReplyDocument(`Opener\n${card}`, card)).toBe(`Opener\n${card}\n`);
    expect(settleReplyDocument(`A${REPLY_QUOTE_MARK}!${REPLY_QUOTE_END}`, "")).toBe("A!");
    const draft: ReplyDraft = { quotes: [quote("a", "A", "Head")], text: "Tail" };
    const woven = weaveReplyDraft(draft);
    expect(settleReplyDocument(woven, woven)).toBe(woven);
  });

  it("keeps a card only when the previous document holds its exact token", () => {
    const card = replyQuoteLine({ id: "a", text: "A" });
    const forged = replyQuoteLine({ id: "forged", text: "Forged" });
    // A value set from code can't forge a card; the text around the foreign token joins up.
    expect(settleReplyDocument(`Head${forged}Tail`, card)).toBe("HeadTail");
    expect(settleReplyDocument(`Head${forged}Tail`, "")).toBe("HeadTail");
    expect(settleReplyDocument(`${forged}`, "")).toBe("");
    expect(settleReplyDocument(card, card)).toBe(`${card}\n`);
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
