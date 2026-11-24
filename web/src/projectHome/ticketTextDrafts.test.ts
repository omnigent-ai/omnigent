import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  readTicketDraft,
  ticketCommentDraftKey,
  useTicketDraft,
  writeTicketDraft,
} from "./ticketTextDrafts";

afterEach(() => window.localStorage.clear());

describe("useTicketDraft", () => {
  it("restores a ticket's draft after remounting and keeps tickets separate", () => {
    const first = ticketCommentDraftKey("project", "ticket-a");
    const second = ticketCommentDraftKey("project", "ticket-b");
    const typed = renderHook(() => useTicketDraft(first));
    act(() => typed.result.current[1]("half-written"));
    typed.unmount();

    expect(renderHook(() => useTicketDraft(first)).result.current[0]).toBe("half-written");
    expect(renderHook(() => useTicketDraft(second)).result.current[0]).toBe("");
  });

  it("clears the stored draft when the text is emptied", () => {
    const key = ticketCommentDraftKey("project", "ticket-a");
    const { result } = renderHook(() => useTicketDraft(key));
    act(() => result.current[1]((previous) => `${previous}note`));
    expect(readTicketDraft(key)).toBe("note");
    act(() => result.current[1]("  "));
    expect(readTicketDraft(key)).toBeNull();
  });

  it("prefers a stored draft over the fallback and ignores storage without a key", () => {
    writeTicketDraft("new:project:title", "saved");
    expect(renderHook(() => useTicketDraft("new:project:title", "x")).result.current[0]).toBe(
      "saved",
    );
    expect(renderHook(() => useTicketDraft(undefined, "x")).result.current[0]).toBe("x");
  });
});
