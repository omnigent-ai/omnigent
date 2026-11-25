import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
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
  it("keeps a card when text is appended to a document that ends on one", () => {
    const { result } = renderHook(() => useReplyDraft(true));
    act(() => result.current.appendQuote("Quoted"));
    act(() => result.current.editText(null, (document) => document.replace(/\n$/, "")));
    act(() => result.current.editText(null, (document) => `${document}@file `));
    expect(result.current.draft.quotes).toHaveLength(1);
    expect(result.current.draft.text).toBe("@file ");
  });
});
