import { describe, expect, it } from "vitest";
import { parseReplyDraft, removeReplyQuote, serializeReplyDraft } from "./replyDraft";

describe("reply drafts", () => {
  it("round-trips interleaved text and multiline quotes", () => {
    const text =
      "Introduction\n\n> First line\n> \n> Next paragraph\n\nFirst answer\n\n> Second quote\n\nSecond answer";
    const draft = parseReplyDraft(text);
    expect(draft.quotes).toMatchObject([
      { before: "Introduction", text: "First line\n\nNext paragraph" },
      { before: "First answer", text: "Second quote" },
    ]);
    expect(draft.text).toBe("Second answer");
    expect(serializeReplyDraft(draft)).toBe(text);
  });

  it.each(["", "Plain text\n\n", "    indented code", "prefix > inline text"])(
    "preserves a plain draft: %j",
    (text) => {
      expect(parseReplyDraft(text)).toEqual({ quotes: [], text });
      expect(serializeReplyDraft(parseReplyDraft(text))).toBe(text);
    },
  );

  it.each(["```", "~~~~"])(
    "does not turn quoted examples in a %s code fence into cards",
    (fence) => {
      const code = `${fence}markdown\n> Example, not a reply\n${fence}`;
      const draft = parseReplyDraft(`${code}\n\n> Real quote\n\nReply`);
      expect(draft.quotes).toMatchObject([{ before: code, text: "Real quote" }]);
      expect(serializeReplyDraft(draft)).toBe(`${code}\n\n> Real quote\n\nReply`);
    },
  );

  it.each([
    ["```", "```not a closing fence"],
    ["~~~~", "~~~~not a closing fence"],
    ["````", "```"],
    ["```", "~~~"],
  ])("keeps code quoted with %s inside an unclosed fence after %s", (fence, example) => {
    const code = `${fence}markdown\n${example}\n> Still a code example\n${fence}`;
    const text = `${code}\n\n> Real quote\n\nReply`;
    const draft = parseReplyDraft(text);
    expect(draft.quotes).toMatchObject([{ before: code, text: "Real quote" }]);
    expect(serializeReplyDraft(draft)).toBe(text);
  });

  it("does not treat backticks in a fence's info string as a code fence", () => {
    const text = "```invalid`info\n\n> Real quote\n\nReply";
    const draft = parseReplyDraft(text);
    expect(draft.quotes).toMatchObject([{ before: "```invalid`info", text: "Real quote" }]);
    expect(serializeReplyDraft(draft)).toBe(text);
  });

  it("preserves indentation in text before a quote", () => {
    const text = "    code example\n\n> Review it\n\nReply";
    expect(serializeReplyDraft(parseReplyDraft(text))).toBe(text);
  });

  it("removes a quote without discarding either adjacent text block", () => {
    const draft = parseReplyDraft("Intro\n\n> First\n\nOne\n\n> Second\n\nTwo");
    const removed = removeReplyQuote(draft, draft.quotes[0]!.id);
    expect(serializeReplyDraft(removed)).toBe("Intro\n\nOne\n\n> Second\n\nTwo");
    expect(serializeReplyDraft(removeReplyQuote(removed, removed.quotes[0]!.id))).toBe(
      "Intro\n\nOne\n\nTwo",
    );
  });
});
