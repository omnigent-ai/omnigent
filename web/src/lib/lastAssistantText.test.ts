import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { extractAssistantText, fetchLastAssistantText, previewText } from "./lastAssistantText";

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
});
