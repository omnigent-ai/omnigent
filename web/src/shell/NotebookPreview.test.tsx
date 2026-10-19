import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AnsiDefault from "ansi-to-react";
import { NotebookPreview, OutputView } from "./NotebookPreview";

// Stub Shiki (same rationale as CodeViewer.test.tsx) — render code verbatim so
// assertions can target cell source without async highlighting.
vi.mock("@/components/ai-elements/code-block", () => ({
  CodeBlockContent: ({ code }: { code: string }) => <pre data-testid="code-cell">{code}</pre>,
}));

// Count the characters each ANSI render parses, delegating to the real component.
const ansiParsed = vi.hoisted(() => ({ chars: 0 }));
vi.mock("ansi-to-react", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  const module = actual.default as Record<string, unknown> | ((props: unknown) => unknown);
  const Real = (typeof module === "function" ? module : module.default) as (props: {
    children?: string;
  }) => unknown;
  const Counting = (props: { children?: string }) => {
    ansiParsed.chars += props.children?.length ?? 0;
    return Real(props);
  };
  return { default: Counting };
});

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

describe("NotebookPreview — markdown links", () => {
  it("opens URLs outside the app window", () => {
    // Same rule as the Markdown preview: a same-window URL navigates the app
    // itself off its server.
    const notebook = {
      nbformat: 4,
      nbformat_minor: 5,
      metadata: {},
      cells: [
        {
          cell_type: "markdown",
          metadata: {},
          source: "[docs](https://example.com/docs), [plan](plan.md) and [below](#usage)",
        },
      ],
    };
    render(<NotebookPreview content={JSON.stringify(notebook)} />);

    const link = screen.getByText("docs");
    expect(link.getAttribute("href")).toBe("https://example.com/docs");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    // Only URLs open outside the app window; relative and in-page links are unchanged.
    for (const [text, href] of [
      ["plan", "plan.md"],
      ["below", "#usage"],
    ]) {
      const unchanged = screen.getByText(text);
      expect(unchanged.getAttribute("href")).toBe(href);
      expect(unchanged.getAttribute("target")).toBeNull();
    }
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

describe("OutputView: reused for chat code run output", () => {
  it("keeps ANSI colors and styles stderr apart", () => {
    const { container } = render(
      <OutputView
        output={{ output_type: "stream", name: "stderr", text: "\u001b[31mred\u001b[0m" }}
      />,
    );
    const pre = container.querySelector("pre")!;
    expect(pre.className).toContain("bg-destructive/10");
    expect(pre.querySelector("span[style*='color']")?.textContent).toBe("red");
  });

  it("scrolls long lines instead of wrapping when asked", () => {
    const { container } = render(
      <OutputView nowrap output={{ output_type: "stream", name: "stdout", text: "x" }} />,
    );
    const pre = container.querySelector("pre")!;
    expect(pre.classList.contains("whitespace-pre")).toBe(true);
    expect(pre.classList.contains("whitespace-pre-wrap")).toBe(false);
  });
});

describe("OutputView: long streamed output", () => {
  const RED_LINE = "x".repeat(99) + "\n";

  it("keeps a color set early on for text far past the first piece", () => {
    const text = "\u001b[31m" + RED_LINE.repeat(100) + "\u001b[0mplain\n";
    const { container } = render(
      <OutputView output={{ output_type: "stream", name: "stdout", text }} />,
    );
    const spans = [...container.querySelectorAll("span")].filter((span) => span.textContent);
    const red = spans.filter((span) => span.getAttribute("style")?.includes("color"));
    expect(red.map((span) => span.textContent).join("")).toBe(RED_LINE.repeat(100));
    expect(container.textContent).toBe(RED_LINE.repeat(100) + "plain\n");
  });

  it("parses only the end again when output grows", () => {
    const base = RED_LINE.repeat(200);
    const { rerender } = render(
      <OutputView output={{ output_type: "stream", name: "stdout", text: base }} />,
    );
    ansiParsed.chars = 0;
    rerender(
      <OutputView output={{ output_type: "stream", name: "stdout", text: base + RED_LINE }} />,
    );
    expect(ansiParsed.chars).toBeLessThan(10_000);
  });
});

describe("OutputView: pieced ANSI renders like one pass", () => {
  // Same interop as NotebookPreview.tsx for the CJS default export.
  const Ansi = ("default" in AnsiDefault ? AnsiDefault.default : AnsiDefault) as typeof AnsiDefault;
  const ESC = "\u001b[";
  const FILLER = ("f".repeat(99) + "\n").repeat(45);

  /** Each character's style, merged into runs, ignoring how spans are split. */
  function styledRuns(root: Element): [string, string][] {
    const runs: [string, string][] = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const style = node.parentElement?.closest("span")?.getAttribute("style") ?? "";
      const last = runs.at(-1);
      if (last && last[0] === style) last[1] += node.textContent;
      else runs.push([style, node.textContent ?? ""]);
    }
    return runs;
  }

  function expectSameAsOnePass(text: string) {
    const single = render(<Ansi>{text}</Ansi>).container;
    const pieced = render(
      <OutputView output={{ output_type: "stream", name: "stdout", text }} />,
    ).container;
    expect(pieced.querySelectorAll("code").length).toBeGreaterThan(1);
    expect(styledRuns(pieced)).toEqual(styledRuns(single));
  }

  it("carries more than 16 active style and color codes across a piece", () => {
    const codes = ["1", "3", "4", "9"].map((code) => `${ESC}${code}m`).join("");
    const recolors = Array.from({ length: 20 }, (_, index) => `${ESC}${31 + (index % 7)}m`);
    expectSameAsOnePass(codes + recolors.join("") + FILLER + "after the boundary\n");
  });

  it("starts over after a reset and carries the new color", () => {
    expectSameAsOnePass(
      `${ESC}1;31mbold red\n` + FILLER + `${ESC}0m${ESC}34mblue\n` + FILLER + "still blue\n",
    );
  });

  it("carries 256-color and truecolor codes, ignoring invalid ones", () => {
    expectSameAsOnePass(
      `${ESC}38;5;196m${ESC}48;2;10;20;30m${ESC}38;5;300m${ESC}48;2;1;2;999m` +
        FILLER +
        "palette fg, truecolor bg\n",
    );
  });

  it("follows partial resets and repeated codes like the one-pass parser", () => {
    expectSameAsOnePass(
      `${ESC}1m${ESC}1m${ESC}2m${ESC}22m${ESC}7;4m${ESC}24m${ESC}39;49m${ESC}93;104m` +
        FILLER +
        "one bold left, reversed, bright colors\n",
    );
  });

  it("applies backspaces before reading the codes a line leaves on", () => {
    expectSameAsOnePass(
      `${ESC}31mx\b${ESC}32m\b\b${ESC}33my\b\b\bz\n` + FILLER + "after the boundary\n",
    );
  });

  it("applies carriage returns before reading the codes a line leaves on", () => {
    expectSameAsOnePass(`${ESC}31mred\r${ESC}32mgreen\n` + FILLER + "green\n");
  });
});
