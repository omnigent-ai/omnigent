import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { serializeReplyDraft, type StoredReplyDraft } from "@/lib/replyDraft";
import { replyQuoteLine } from "@/lib/replyDraftDocument";
import { useReplyDraft } from "./useReplyDraft";

describe("useReplyDraft in single-document mode", () => {
  it("ignores an edit addressed to a quote, rather than replacing the document", () => {
    const { result } = renderHook(() => useReplyDraft(true));
    act(() => result.current.appendQuote("Quoted"));
    const before = result.current.fullText;
    act(() => result.current.editText("stray-id", "replaced"));
    expect(result.current.fullText).toBe(before);
    expect(result.current.draft.quotes).toHaveLength(1);
  });
  it("writes only the tail, so a command's text keeps the quotes and the text before them", () => {
    const { result } = renderHook(() => useReplyDraft(true));
    act(() => result.current.replaceText("Opener", undefined));
    act(() => result.current.appendQuote("Quoted"));
    act(() => result.current.setValue("/side "));
    expect(result.current.draft.quotes).toHaveLength(1);
    expect(result.current.draft.quotes[0]!.text).toBe("Quoted");
    expect(result.current.draft.text).toBe("/side ");
    act(() => result.current.setValue(""));
    expect(result.current.draft.quotes).toHaveLength(1);
    expect(result.current.draft.text).toBe("");
  });

  it("takes the editor's own document as it stands, keeping the cards it holds", () => {
    const { result } = renderHook(() => useReplyDraft(true));
    const card = replyQuoteLine({ id: "kept", text: "Kept" });
    act(() => result.current.setEditorDocument(`Head\n${card}\nTail`));
    expect(result.current.draft.quotes).toEqual([{ id: "kept", before: "Head", text: "Kept" }]);
    expect(result.current.draft.text).toBe("Tail");
    expect(result.current.replyDocument).toBe(`Head\n${card}\nTail`);
    // In one document, `value` is the tail that writes address; the whole document is replyDocument.
    expect(result.current.value).toBe("Tail");
  });

  it("keeps a card when text is appended to a document that ends on one", () => {
    const { result } = renderHook(() => useReplyDraft(true));
    act(() => result.current.appendQuote("Quoted"));
    act(() => result.current.editText(null, (document) => document.replace(/\n$/, "")));
    act(() => result.current.editText(null, (document) => `${document}@file `));
    expect(result.current.draft.quotes).toHaveLength(1);
    expect(result.current.draft.text).toBe("@file ");
  });
});

describe("useReplyDraft quote removal and mode", () => {
  it.each([true, false])(
    "removes a card with its blank separator, as the textarea does (single document: %s)",
    (singleDocument) => {
      const { result } = renderHook(() => useReplyDraft(singleDocument));
      const saved: StoredReplyDraft = {
        version: 1,
        quotes: [
          { before: "A", text: "First" },
          { before: "", text: "Second" },
        ],
        text: "B",
      };
      act(() => result.current.replaceText(serializeReplyDraft(saved), saved));
      const [first, second] = result.current.draft.quotes;
      expect(result.current.replyDocument).toBe(
        `A\n${replyQuoteLine(first!)}\n\n${replyQuoteLine(second!)}\nB`,
      );
      act(() => result.current.removeQuote(second!.id));
      expect(result.current.draft).toEqual({ quotes: [first], text: "B" });
    },
  );

  it("replaces the draft and drops the editor's typed document", () => {
    const { result } = renderHook(() => useReplyDraft(true));
    act(() => result.current.appendQuote("Quoted"));
    act(() => result.current.editText(null, (document) => `${document}Typed`));
    expect(result.current.replyDocument).toContain("Typed");
    act(() => result.current.replaceText("New"));
    expect(result.current.replyDocument).toBe("New");
    expect(result.current.draft).toEqual({ quotes: [], text: "New" });
  });

  it("keeps the draft when the single-document flag changes mid-draft", () => {
    const { result, rerender } = renderHook(({ singleDocument }) => useReplyDraft(singleDocument), {
      initialProps: { singleDocument: true },
    });
    act(() => result.current.appendQuote("Quoted"));
    const [quote] = result.current.draft.quotes;
    rerender({ singleDocument: false });
    expect(result.current.draft.quotes).toEqual([quote]);
    act(() => result.current.editText(null, "Tail"));
    expect(result.current.value).toBe("Tail");
    rerender({ singleDocument: true });
    expect(result.current.replyDocument).toBe(`${replyQuoteLine(quote!)}\nTail`);
    expect(result.current.draft.text).toBe("Tail");
  });
});
