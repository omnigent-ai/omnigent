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

    // The keyword and the literal end up in distinct tokens.
    expect(screen.getByText("const")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
  });
});
