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

  it("strips all whitespace from base64 images (CRLF-split payloads stay valid)", () => {
    // A payload split across array lines with CRLF endings and a stray space —
    // a data-URI containing any whitespace is rejected by the browser.
    const nb = JSON.stringify({
      nbformat: 4,
      cells: [
        {
          cell_type: "code",
          execution_count: 1,
          source: [],
          outputs: [
            {
              output_type: "display_data",
              data: { "image/png": ["iVBORw0K\r\n", "GgoA AAAN\n"] },
            },
          ],
        },
      ],
    });
    const { container } = render(<NotebookPreview content={nb} />);
    const src = container.querySelector("img")!.getAttribute("src")!;
    expect(src).toBe("data:image/png;base64,iVBORw0KGgoAAAAN");
    expect(src).not.toMatch(/\s/);
  });

  it("falls back to a note (not a broken img) for corrupt base64 images", () => {
    // length % 4 === 1 is never valid base64 — a browser rejects the data-URI
    // with ERR_INVALID_URL. Show the text/plain repr with a note instead.
    const nb = JSON.stringify({
      nbformat: 4,
      cells: [
        {
          cell_type: "code",
          execution_count: 1,
          source: [],
          outputs: [
            {
              output_type: "display_data",
              data: {
                "image/png": "not@valid#base64!",
                "text/plain": "<Figure size 640x480 with 1 Axes>",
              },
            },
          ],
        },
      ],
    });
    const { container } = render(<NotebookPreview content={nb} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/could not be decoded/)).toBeInTheDocument();
    expect(screen.getByText(/Figure size 640x480/)).toBeInTheDocument();
  });
});

describe("NotebookPreview — edge cases", () => {
  it("renders error tracebacks (ANSI codes handled, not shown raw)", () => {
    const { container } = render(<NotebookPreview content={edgecasesRaw} />);
    expect(screen.getAllByText(/ZeroDivisionError/).length).toBeGreaterThan(0);
    // ansi-to-react must consume the escape sequences, not print them.
    expect(container.textContent).not.toContain("[0;31m");
    // Long unbroken traceback runs (separator rules, paths) must scroll within
    // the cell, not widen the layout — the output <pre> owns the overflow.
    const tb = screen.getAllByText(/ZeroDivisionError/)[0].closest("pre");
    expect(tb!.className).toContain("overflow-x-auto");
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

  it("recovers from raw control characters (unescaped ANSI) in cell output", () => {
    // A notebook whose stream output carries a bare ESC (0x1B) and newline —
    // strict JSON.parse rejects these, but the preview escapes and recovers.
    const nb = `{"nbformat": 4, "cells": [
      {"cell_type": "code", "execution_count": 1, "source": ["print(x)"],
       "outputs": [{"output_type": "stream", "name": "stdout",
         "text": ["\x1b[31mred\x1b[0m line one\nline two"]}]}
    ]}`;
    render(<NotebookPreview content={nb} />);
    // Parse recovered (no error state) and the output text is rendered.
    expect(screen.queryByText(/Cannot render notebook/)).toBeNull();
    expect(screen.getByText(/line one/)).toBeInTheDocument();
  });
});
