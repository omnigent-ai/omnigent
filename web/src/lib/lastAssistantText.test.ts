import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  extractAssistantText,
  extractErrorText,
  fetchLastAssistantText,
  latestOutput,
  previewText,
} from "./lastAssistantText";

describe("extractAssistantText", () => {
  it("joins output_text blocks from an assistant message", () => {
    const item = {
      type: "message",
      role: "assistant",
      content: [
        { type: "output_text", text: "Hello " },
        { type: "output_text", text: "world" },
      ],
    };
    expect(extractAssistantText(item)).toBe("Hello world");
  });

  it("returns undefined for a user message", () => {
    const item = {
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: "hi" }],
    };
    expect(extractAssistantText(item)).toBeUndefined();
  });

  it("returns undefined for a non-message item (e.g. tool call)", () => {
    expect(extractAssistantText({ type: "function_call", name: "x" })).toBeUndefined();
  });

  it("returns undefined when the assistant message has no text blocks", () => {
    const item = { type: "message", role: "assistant", content: [{ type: "input_image" }] };
    expect(extractAssistantText(item)).toBeUndefined();
  });

  it("is tolerant of malformed input", () => {
    expect(extractAssistantText(null)).toBeUndefined();
    expect(extractAssistantText(undefined)).toBeUndefined();
    expect(extractAssistantText("nope")).toBeUndefined();
    expect(extractAssistantText({ type: "message", role: "assistant" })).toBeUndefined();
  });
});

describe("extractErrorText", () => {
  it("prefers the classified title over the raw message", () => {
    const item = { type: "error", title: "Rate limited", message: "429 Too Many Requests" };
    expect(extractErrorText(item)).toBe("Rate limited");
  });

  it("falls back to the raw message", () => {
    expect(extractErrorText({ type: "error", message: " boom " })).toBe("boom");
  });

  it("ignores info-level notices and non-error items", () => {
    // An info notice renders as a neutral pill; it doesn't mean the turn failed.
    expect(extractErrorText({ type: "error", level: "info", message: "fyi" })).toBeUndefined();
    expect(extractErrorText({ type: "message", role: "assistant" })).toBeUndefined();
    expect(extractErrorText(null)).toBeUndefined();
  });
});

describe("latestOutput", () => {
  const reply = (text: string, id?: string) => ({
    ...(id === undefined ? {} : { id }),
    type: "message",
    role: "assistant",
    content: [{ type: "output_text", text }],
  });

  it("returns the newest assistant reply, stepping past trailing tool items", () => {
    const items = [
      reply("First answer."),
      { type: "message", role: "user", content: [{ type: "input_text", text: "more" }] },
      reply("Second answer."),
      { type: "function_call_output", output: "tool result" },
    ];
    expect(latestOutput(items)?.preview).toBe("Second answer.");
  });

  it("surfaces a failed turn's error when it is newer than the last reply", () => {
    const items = [reply("Earlier answer."), { type: "error", message: "Model overloaded" }];
    expect(latestOutput(items)?.preview).toBe("Model overloaded");
  });

  it("returns undefined when nothing carries output text", () => {
    expect(latestOutput([{ type: "function_call", name: "edit" }])).toBeUndefined();
  });

  it("keys the output by its item id", () => {
    // WHY: two answers with the same text are still different replies.
    const items = [reply("Done.", "item_1"), reply("Done.", "item_2")];
    expect(latestOutput(items)).toEqual({ key: "item_2", preview: "Done." });
  });

  it("keys an error item by its id too", () => {
    const items = [reply("Done.", "item_1"), { id: "item_err", type: "error", message: "boom" }];
    expect(latestOutput(items)).toEqual({ key: "item_err", preview: "boom" });
  });

  it("falls back to the full text when the item has no id", () => {
    // The key is the untruncated text, so two long replies sharing a prefix differ.
    const long = `${"a".repeat(30)} first`;
    expect(latestOutput([reply(long)])?.key).toBe(long);
  });

  it("returns the untrimmed text only when the preview cut some of it", () => {
    const capped = `${"a".repeat(30)} first`;
    expect(latestOutput([reply(capped)], 10)).toEqual({
      key: capped,
      preview: "aaaaaaaaa…",
      fullText: capped,
    });
    const manyLines = "one\ntwo\nthree\nfour";
    expect(latestOutput([reply(manyLines)])?.fullText).toBe(manyLines);
    // Blank lines between paragraphs are dropped from the preview but lose no text.
    expect(latestOutput([reply("one\n\ntwo")])).toEqual({ key: "one\n\ntwo", preview: "one\ntwo" });
  });
});

describe("previewText", () => {
  it("keeps short single-line text intact", () => {
    expect(previewText("All done.")).toBe("All done.");
  });

  it("drops blank lines and caps the number of lines", () => {
    const text = "line1\n\n  line2  \nline3\nline4";
    expect(previewText(text, 200, 3)).toBe("line1\nline2\nline3");
  });

  it("truncates to the char budget with an ellipsis", () => {
    const out = previewText("a".repeat(300), 10);
    expect(out.endsWith("…")).toBe(true);
    expect(out.length).toBe(10);
  });
});

describe("fetchLastAssistantText", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function okResponse(data: unknown[]): Response {
    return { ok: true, json: async () => ({ data }) } as unknown as Response;
  }

  it("returns the first assistant text scanning newest-first", async () => {
    // order=desc: newest item first. A trailing tool call precedes the
    // assistant message; we should skip it and find the message.
    fetchMock.mockResolvedValue(
      okResponse([
        { type: "function_call_output", output: "tool result" },
        {
          type: "message",
          role: "assistant",
          content: [{ type: "output_text", text: "Fixed the badge bug." }],
        },
      ]),
    );
    expect(await fetchLastAssistantText("conv_a")).toBe("Fixed the badge bug.");
    // Hits the session items endpoint with desc order.
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("/v1/sessions/conv_a/items");
    expect(url).toContain("order=desc");
  });

  it("returns undefined when there is no trailing assistant text", async () => {
    fetchMock.mockResolvedValue(okResponse([{ type: "function_call", name: "edit" }]));
    expect(await fetchLastAssistantText("conv_a")).toBeUndefined();
  });

  it("returns undefined on a non-ok response", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500 } as unknown as Response);
    expect(await fetchLastAssistantText("conv_a")).toBeUndefined();
  });

  it("returns undefined when fetch throws", async () => {
    fetchMock.mockRejectedValue(new Error("network"));
    expect(await fetchLastAssistantText("conv_a")).toBeUndefined();
  });

  it("prefixes the items endpoint with the configured base path", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    try {
      fetchMock.mockResolvedValue(okResponse([]));
      await fetchLastAssistantText("conv_a");
      const url = fetchMock.mock.calls[0][0] as string;
      expect(url).toContain("/proxy/6767/v1/sessions/conv_a/items");
    } finally {
      delete window.__OMNIGENT_BASE_PATH__;
    }
  });
});
