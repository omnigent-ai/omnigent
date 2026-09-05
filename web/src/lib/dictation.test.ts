// Tests for the dictation socket protocol parsing. The DictationSession
// transport itself (mic + AudioWorklet + WebSocket) can't run in jsdom;
// its behavior against the component is pinned in ComposerMicButton.test.tsx
// with a mocked session, and the full loop runs in the Playwright e2e test
// against the server's fake engine.

import { afterEach, describe, expect, it, vi } from "vitest";
import { parseDictationEvent, restoreDictationPunctuation } from "./dictation";

afterEach(() => vi.unstubAllGlobals());

describe("parseDictationEvent", () => {
  it("parses the transcript event shapes", () => {
    expect(parseDictationEvent('{"type":"ready"}')).toEqual({ type: "ready" });
    expect(parseDictationEvent('{"type":"partial","text":"hel"}')).toEqual({
      type: "partial",
      text: "hel",
    });
    expect(parseDictationEvent('{"type":"final","text":"hello."}')).toEqual({
      type: "final",
      text: "hello.",
    });
    expect(parseDictationEvent('{"type":"stopped","text":""}')).toEqual({
      type: "stopped",
      text: "",
    });
    expect(parseDictationEvent('{"type":"error","message":"boom"}')).toEqual({
      type: "error",
      message: "boom",
    });
  });

  it("returns null for malformed or unknown frames", () => {
    expect(parseDictationEvent("not json")).toBeNull();
    expect(parseDictationEvent("42")).toBeNull();
    expect(parseDictationEvent("null")).toBeNull();
    expect(parseDictationEvent('{"type":"future-thing"}')).toBeNull();
    // Known types with a missing/mistyped payload are dropped, not crashed on.
    expect(parseDictationEvent('{"type":"partial"}')).toBeNull();
    expect(parseDictationEvent('{"type":"partial","text":7}')).toBeNull();
    expect(parseDictationEvent('{"type":"error"}')).toBeNull();
  });
});

describe("restoreDictationPunctuation", () => {
  it("posts a completed transcript and returns display-ready text", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ text: "你好，世界！" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(restoreDictationPunctuation("你好世界")).resolves.toBe("你好，世界！");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/dictation/punctuation",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ text: "你好世界" }),
      }),
    );
  });

  it("rejects malformed responses so the composer can preserve raw text", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
    await expect(restoreDictationPunctuation("你好世界")).rejects.toThrow("did not contain text");
  });
});
