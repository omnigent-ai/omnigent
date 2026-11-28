import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { createHighlighter, codeToTokens } = vi.hoisted(() => ({
  createHighlighter: vi.fn(),
  codeToTokens: vi.fn(),
}));

vi.mock("shiki", () => ({ createHighlighter }));

let highlightCode: typeof import("./code-block").highlightCode;

beforeEach(async () => {
  vi.resetModules();
  createHighlighter.mockReset();
  codeToTokens.mockReset();
  codeToTokens.mockImplementation((code: string) => ({
    fg: "inherit",
    bg: "transparent",
    tokens: [[{ content: code }]],
  }));
  createHighlighter.mockResolvedValue({
    getLoadedLanguages: () => ["typescript", "python"],
    codeToTokens,
  });
  ({ highlightCode } = await import("./code-block"));
});

afterEach(() => vi.restoreAllMocks());

function highlight(code: string, language: "typescript" | "python" = "typescript") {
  return new Promise<NonNullable<ReturnType<typeof highlightCode>>>((resolve) => {
    const cached = highlightCode(code, language, resolve);
    if (cached) resolve(cached);
  });
}

describe("code highlighting cache", () => {
  it("shares one tokenization across rendering and concurrent subscribers", async () => {
    expect(highlightCode("const answer = 42;", "typescript")).toBeNull();
    const first = highlight("const answer = 42;");
    const second = highlight("const answer = 42;");

    expect(await first).toBe(await second);
    expect(codeToTokens).toHaveBeenCalledTimes(1);
    const callback = vi.fn();
    expect(highlightCode("const answer = 42;", "typescript", callback)).toBe(await first);
    expect(callback).not.toHaveBeenCalled();
  });

  it("distinguishes edits in the middle of equal-length sources and their languages", async () => {
    const prefix = "// shared header\n".repeat(10);
    const suffix = "// shared footer\n".repeat(10);
    const original = `${prefix}const value = 1;\n${suffix}`;
    const edited = `${prefix}const value = 2;\n${suffix}`;

    await highlight(original);
    const changed = await highlight(edited);
    expect(changed.tokens[0][0].content).toBe(edited);
    await highlight(edited, "python");
    expect(codeToTokens).toHaveBeenCalledTimes(3);
  });

  it("evicts old blocks after many small snippets while preserving recently read ones", async () => {
    const recent = await highlight("recent");
    await highlight("oldest");
    for (let index = 0; index < 127; index++) {
      expect(highlightCode("recent", "typescript")).toBe(recent);
      await highlight(`const value${index} = ${index};`);
    }

    expect(highlightCode("recent", "typescript")).toBe(recent);
    expect(highlightCode("oldest", "typescript")).toBeNull();
    await highlight("oldest");
  });

  it("bounds retained source size and does not retain an oversized result", async () => {
    await highlight("oldest");
    for (let index = 0; index < 32; index++) {
      await highlight(`${index}:${"x".repeat(16 * 1024)}`);
    }
    expect(highlightCode("oldest", "typescript")).toBeNull();
    await highlight("oldest");

    const oversized = "x".repeat(512 * 1024);
    const result = await highlight(oversized);
    expect(result.tokens[0][0].content).toBe(oversized);
    const callsBeforeRetry = codeToTokens.mock.calls.length;
    await highlight(oversized);
    expect(codeToTokens).toHaveBeenCalledTimes(callsBeforeRetry + 1);
  });

  it("retries a failed highlighter load and releases the pending request", async () => {
    const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
    createHighlighter.mockRejectedValueOnce(new Error("unavailable grammar"));
    expect(highlightCode("retry", "typescript")).toBeNull();
    await vi.waitFor(() => expect(errorLog).toHaveBeenCalled());

    await highlight("retry");
    expect(createHighlighter).toHaveBeenCalledTimes(2);
    expect(codeToTokens).toHaveBeenCalledTimes(1);
  });
});
