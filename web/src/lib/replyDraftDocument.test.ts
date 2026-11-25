import { describe, expect, it } from "vitest";
import {
  appendQuoteToDocument,
  hasReplyQuoteLines,
  mapComposerSelection,
  parseReplyQuoteLine,
  removeReplyQuoteLine,
  replaceTrailingText,
  replyQuoteClipboardText,
  replyQuoteLineIndex,
  REPLY_QUOTE_MARK,
  typedDocumentFor,
  unweaveReplyDraft,
  weaveReplyDraft,
  withoutReplyQuoteMarks,
} from "./replyDraftDocument";
import {
  appendReplyQuote,
  removeReplyQuote,
  serializeReplyDraft,
  type ReplyDraft,
} from "./replyDraft";

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

describe("reply draft document edits", () => {
  const q = (id: string, before: string, text: string) => ({ id, before, text });
  const drafts: ReplyDraft[] = [
    { quotes: [q("a", "", "One")], text: "Reply" },
    { quotes: [q("a", "X", "One"), q("b", "", "Two")], text: "tail" },
    { quotes: [q("a", "X", "One"), q("b", "Y", "Two")], text: "" },
    { quotes: [q("a", "", "One"), q("b", "", "Two")], text: "end" },
    { quotes: [q("a", "Head\nmore", "One")], text: "" },
  ];

  it("removes a quote line the way removeReplyQuote does, for every position", () => {
    for (const draft of drafts) {
      draft.quotes.forEach((quote) => {
        const lineIndex = replyQuoteLineIndex(weaveReplyDraft(draft), quote.id);
        expect(removeReplyQuoteLine(weaveReplyDraft(draft), lineIndex)).toBe(
          weaveReplyDraft(removeReplyQuote(draft, quote.id)),
        );
      });
    }
  });

  it("appends a quote to a document the way weaving the appended draft does", () => {
    for (const draft of drafts) {
      const next = appendReplyQuote(draft, "New");
      const newest = next.quotes[next.quotes.length - 1]!;
      expect(appendQuoteToDocument(weaveReplyDraft(draft), newest)).toBe(weaveReplyDraft(next));
    }
  });

  it("replaces only the trailing text of a document, keeping the lines above the last quote", () => {
    const draft: ReplyDraft = { quotes: [q("a", "Head", "One")], text: "old" };
    expect(replaceTrailingText(weaveReplyDraft(draft), "new")).toBe(
      weaveReplyDraft({ ...draft, text: "new" }),
    );
    expect(replaceTrailingText("plain", "new")).toBe("new");
  });

  it("keeps the typed document while it describes the draft, and weaves it otherwise", () => {
    const draft: ReplyDraft = {
      quotes: [q("a", "", "One"), q("b", "", "Two")],
      text: "end",
    };
    // Weaving writes a blank line before the second quote; the user deleted it, which changes nothing in the draft.
    const [, first, , second, end] = weaveReplyDraft(draft).split("\n");
    const typed = ["", first, second, end].join("\n");
    expect(typedDocumentFor(draft, typed)).toBe(typed);
    expect(typedDocumentFor(draft, null)).toBe(weaveReplyDraft(draft));
    expect(typedDocumentFor({ ...draft, text: "changed" }, typed)).toBe(
      weaveReplyDraft({ ...draft, text: "changed" }),
    );
  });

  it("gives a repeated quote id a fresh id, so each card keeps its own", () => {
    const token = weaveReplyDraft({ quotes: [q("dup", "", "One")], text: "" });
    const draft = unweaveReplyDraft(`${token}\n${token}\nend`);
    expect(draft.quotes).toHaveLength(2);
    expect(draft.quotes[0]!.id).toBe("dup");
    expect(draft.quotes[1]!.id).not.toBe("dup");
  });

  it("maps a textarea caret into the document, and a document offset back to its input", () => {
    const document = weaveReplyDraft({ quotes: [q("a", "Intro", "One")], text: "Reply" });
    const tail = document.length - "Reply".length;
    expect(
      mapComposerSelection(
        document,
        { inputId: "tail", selectionStart: 2, selectionEnd: 4 },
        "textarea",
        "tail",
      ),
    ).toEqual({ inputId: "tail", selectionStart: tail + 2, selectionEnd: tail + 4 });
    expect(
      mapComposerSelection(
        document,
        { inputId: "a", selectionStart: 3, selectionEnd: 3 },
        "textarea",
        "tail",
      ),
    ).toEqual({ inputId: "tail", selectionStart: 3, selectionEnd: 3 });
    expect(
      mapComposerSelection(
        document,
        { inputId: "tail", selectionStart: tail + 1, selectionEnd: tail + 3 },
        "codemirror",
        "tail",
      ),
    ).toEqual({ inputId: "tail", selectionStart: 1, selectionEnd: 3 });
    expect(
      mapComposerSelection(
        document,
        { inputId: "tail", selectionStart: 3, selectionEnd: 3 },
        "codemirror",
        "tail",
      ),
    ).toEqual({ inputId: "a", selectionStart: 3, selectionEnd: 3 });
  });

  it("maps an offset in a quote's line to the end of its text, and collapses a selection across inputs", () => {
    const document = weaveReplyDraft({ quotes: [q("a", "Intro", "One")], text: "Reply" });
    // The offset at the quote's own line lands at the end of the text before it.
    expect(
      mapComposerSelection(
        document,
        {
          inputId: "tail",
          selectionStart: "Intro\n".length + 1,
          selectionEnd: "Intro\n".length + 1,
        },
        "codemirror",
        "tail",
      ),
    ).toEqual({ inputId: "a", selectionStart: 5, selectionEnd: 5 });
    // A selection from one input to another collapses to its start.
    expect(
      mapComposerSelection(
        document,
        { inputId: "tail", selectionStart: 2, selectionEnd: document.length },
        "codemirror",
        "tail",
      ),
    ).toEqual({ inputId: "a", selectionStart: 2, selectionEnd: 2 });
  });
});
