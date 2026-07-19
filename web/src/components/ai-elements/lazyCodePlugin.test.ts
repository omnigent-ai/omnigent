import { describe, expect, it, vi } from "vitest";
import { lazyCodePlugin } from "./lazyCodePlugin";

describe("lazyCodePlugin — deferred Shiki engine", () => {
  it("satisfies the Streamdown code-highlighter plugin contract", () => {
    expect(lazyCodePlugin.name).toBe("shiki");
    expect(lazyCodePlugin.type).toBe("code-highlighter");
  });

  it("returns default themes synchronously before the engine loads", () => {
    // getThemes() is called on the render path, so it must resolve without
    // waiting for the lazily-imported @streamdown/code module.
    expect(lazyCodePlugin.getThemes()).toEqual(["github-light", "github-dark"]);
  });

  it("returns null on the first highlight and resolves tokens via callback", async () => {
    const result = await new Promise<{ tokens: unknown[][] }>((resolve) => {
      // First call must be non-blocking: null now, real tokens through the
      // callback once the engine finishes loading.
      const immediate = lazyCodePlugin.highlight(
        {
          code: "const answer = 42;",
          language: "typescript",
          themes: ["github-light", "github-dark"],
        },
        (highlighted) => resolve(highlighted),
      );
      expect(immediate).toBeNull();
    });

    expect(Array.isArray(result.tokens)).toBe(true);
    expect(result.tokens.length).toBeGreaterThan(0);
  });

  it("invokes the highlight callback exactly once", async () => {
    const callback = vi.fn();

    lazyCodePlugin.highlight(
      {
        code: "const doubled = 1;",
        language: "typescript",
        themes: ["github-light", "github-dark"],
      },
      callback,
    );

    // Wait past the lazy import + tokenization, then give a couple more
    // microtask/macrotask turns so any stray second invocation would land.
    await vi.waitFor(() => expect(callback).toHaveBeenCalled(), { timeout: 10000 });
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(callback).toHaveBeenCalledTimes(1);
  });
});
