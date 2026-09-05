import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { CodeBlock } from "./code-block";

afterEach(cleanup);

describe("CodeBlock — lazy Shiki highlighting", () => {
  it("renders the raw code immediately before the highlighter loads", () => {
    render(<CodeBlock code={"const answer = 42;"} language="typescript" />);

    // Raw tokens render synchronously so the code is visible without waiting
    // for the lazily-imported Shiki engine.
    expect(screen.getByText(/const answer = 42;/)).toBeTruthy();
    expect(document.querySelector('[data-code-highlighted="false"]')).toBeTruthy();
  });

  it("highlights the code with Shiki after the lazy import resolves", async () => {
    const { container } = render(<CodeBlock code={"const answer = 42;"} language="typescript" />);

    // Once the dynamically-imported highlighter tokenizes the code, Shiki
    // splits the source into per-token spans with inline colors. The raw
    // pre-highlight path renders each line as a single span with no color.
    await waitFor(
      () => {
        const colored = container.querySelectorAll("span[style*='color']");
        expect(colored.length).toBeGreaterThan(1);
      },
      { timeout: 10000 },
    );

    expect(container.querySelector('[data-code-highlighted="true"]')).toBeTruthy();
    // The keyword and the literal end up in distinct tokens.
    expect(screen.getByText("const")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
  });
});

describe("CodeBlock — soft wrap", () => {
  it("keeps white-space: pre by default so columns stay aligned", () => {
    const { container } = render(<CodeBlock code={"a  b  c"} language="text" />);

    const pre = container.querySelector("pre");
    expect(pre).toBeTruthy();
    expect(pre!.className).not.toContain("whitespace-pre-wrap");
  });

  it("soft-wraps long lines when wrap is set, so nothing scrolls horizontally", () => {
    const longLine = "x".repeat(2000);
    const { container } = render(<CodeBlock code={longLine} language="text" wrap />);

    // whitespace-pre-wrap allows breaking at whitespace; wrap-anywhere adds a
    // break opportunity inside unbroken runs (long paths, hashes, URLs) so a
    // single giant token still fits the panel width.
    const pre = container.querySelector("pre");
    expect(pre).toBeTruthy();
    expect(pre!.className).toContain("whitespace-pre-wrap");
    expect(pre!.className).toContain("wrap-anywhere");
  });
});
