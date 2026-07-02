import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NotebookPreview } from "./NotebookPreview";

// Stub Shiki (same rationale as CodeViewer.test.tsx) — render code verbatim so
// assertions can target cell source without async highlighting.
vi.mock("@/components/ai-elements/code-block", () => ({
  CodeBlockContent: ({ code }: { code: string }) => <pre data-testid="code-cell">{code}</pre>,
}));

import typicalRaw from "./__fixtures__/01_typical.ipynb?raw";
import edgecasesRaw from "./__fixtures__/02_edgecases.ipynb?raw";
import brokenRaw from "./__fixtures__/03_broken.ipynb?raw";

afterEach(cleanup);

describe("NotebookPreview — typical notebook", () => {
  it("renders markdown cells as formatted markdown", () => {
    render(<NotebookPreview content={typicalRaw} />);
    expect(screen.getByRole("heading", { name: "Sales Analysis" })).toBeInTheDocument();
    // **Q3 data** renders as <strong>, proving markdown is not shown raw.
    expect(screen.getByText("Q3 data").tagName).toBe("STRONG");
  });

  it("renders code cells with source and execution counts", () => {
    render(<NotebookPreview content={typicalRaw} />);
    expect(screen.getAllByTestId("code-cell")[0]).toHaveTextContent("import pandas as pd");
    expect(screen.getByText("In [1]:")).toBeInTheDocument();
    expect(screen.getByText("In [3]:")).toBeInTheDocument();
  });

  it("renders stream output text", () => {
    render(<NotebookPreview content={typicalRaw} />);
    expect(screen.getByText(/rows: 1523/)).toBeInTheDocument();
  });

  it("suppresses text/html output and falls back to text/plain with a note", () => {
    const { container } = render(<NotebookPreview content={typicalRaw} />);
    // The DataFrame html table must NOT be injected into the DOM …
    expect(container.querySelector("table")).toBeNull();
    // … the plain-text repr is shown instead, with an explanatory note.
    expect(screen.getByText(/count\s+1523\.0/)).toBeInTheDocument();
    expect(screen.getByText(/Rich HTML output hidden/)).toBeInTheDocument();
  });

  it("renders image/png output as an inert data-URI img", () => {
    const { container } = render(<NotebookPreview content={typicalRaw} />);
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img!.getAttribute("src")).toMatch(/^data:image\/png;base64,/);
  });
});

describe("NotebookPreview — edge cases", () => {
  it("renders error tracebacks (ANSI codes handled, not shown raw)", () => {
    const { container } = render(<NotebookPreview content={edgecasesRaw} />);
    expect(screen.getAllByText(/ZeroDivisionError/).length).toBeGreaterThan(0);
    // ansi-to-react must consume the escape sequences, not print them.
    expect(container.textContent).not.toContain("[0;31m");
  });

  it("never executes or injects script from hostile text/html outputs", () => {
    const { container } = render(<NotebookPreview content={edgecasesRaw} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText(/Rich HTML output hidden/)).toBeInTheDocument();
  });

  it("renders stderr streams distinctly from stdout", () => {
    render(<NotebookPreview content={edgecasesRaw} />);
    const stderr = screen.getByText(/warning: deprecated/).closest("pre");
    const stdout = screen.getByText("done").closest("pre");
    expect(stderr!.className).toContain("bg-destructive");
    expect(stdout!.className).not.toContain("bg-destructive");
  });

  it("renders raw cells verbatim and empty execution counts as In [ ]", () => {
    render(<NotebookPreview content={edgecasesRaw} />);
    expect(screen.getByText(/raw cell content/)).toBeInTheDocument();
    expect(screen.getByText(/In \[ \]:/)).toBeInTheDocument();
  });
});

describe("NotebookPreview — invalid input", () => {
  it("shows a parse error with a pointer to the source view", () => {
    render(<NotebookPreview content={brokenRaw} />);
    expect(screen.getByText(/Cannot render notebook/)).toBeInTheDocument();
    expect(screen.getByText(/source view/)).toBeInTheDocument();
  });

  it("rejects valid JSON that is not a notebook", () => {
    render(<NotebookPreview content='{"foo": 1}' />);
    expect(screen.getByText(/missing cells array/)).toBeInTheDocument();
  });
});
