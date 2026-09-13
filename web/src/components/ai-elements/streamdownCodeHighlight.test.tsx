import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MessageResponse } from "./message";

afterEach(cleanup);

// Exercises the lazy code plugin THROUGH the real Streamdown markdown path:
// MessageResponse renders <Streamdown plugins={STREAMDOWN_PLUGINS} …> where
// STREAMDOWN_PLUGINS.code is our lazyCodePlugin. This proves that when
// highlight() returns null and later resolves via callback, Streamdown
// re-renders the fenced code block with syntax-highlighted tokens.
describe("Streamdown code highlighting via lazyCodePlugin", () => {
  const MARKDOWN = "```ts\nconst answer = 42;\n```";

  it("shows the raw code immediately, before the lazy Shiki import resolves", () => {
    const { container } = render(<MessageResponse>{MARKDOWN}</MessageResponse>);

    // The code text is present right away (raw, unhighlighted) — highlighting
    // must never block first paint.
    expect(container.textContent).toContain("const answer = 42;");
  });

  it("re-renders with syntax-highlighted token spans after the callback fires", async () => {
    const { container } = render(<MessageResponse>{MARKDOWN}</MessageResponse>);

    // Once the lazily-imported @streamdown/code engine tokenizes and fires the
    // callback, Streamdown re-renders each token as its own span carrying a
    // per-token Shiki color via the `--sdm-c` CSS custom property. Before that
    // the raw path emits a single uncolored line span.
    const coloredSelector = "span[style*='--sdm-c']";
    await waitFor(
      () => {
        expect(container.querySelectorAll(coloredSelector).length).toBeGreaterThan(1);
      },
      { timeout: 10000 },
    );

    // The keyword and the numeric literal land in distinct highlighted tokens.
    const tokenText = Array.from(container.querySelectorAll(coloredSelector)).map(
      (el) => el.textContent,
    );
    expect(tokenText).toContain("const");
    expect(tokenText).toContain("42");
  }, 15000);
});
