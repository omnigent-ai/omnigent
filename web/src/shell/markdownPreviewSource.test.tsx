import { render } from "@testing-library/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { describe, expect, it } from "vitest";
import {
  previewCaretToSource,
  previewRuns,
  previewSelectionToSource,
  rehypeSourceOffsets,
  sourceRangeToPreviewRanges,
} from "./markdownPreviewSource";

function renderPreview(source: string): HTMLElement {
  const { container } = render(
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSourceOffsets]}>
      {source}
    </ReactMarkdown>,
  );
  return container;
}

/** A Range over the first occurrence of `text` in the rendered preview. */
function selectRendered(root: HTMLElement, text: string): Range {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  const joined = nodes.map((node) => node.data).join("");
  const start = joined.indexOf(text);
  if (start < 0) throw new Error(`"${text}" isn't rendered`);
  const end = start + text.length;
  const range = document.createRange();
  let seen = 0;
  for (const node of nodes) {
    const next = seen + node.data.length;
    if (start >= seen && start <= next) range.setStart(node, start - seen);
    if (end >= seen && end <= next) {
      range.setEnd(node, end - seen);
      break;
    }
    seen = next;
  }
  return range;
}

describe("rehypeSourceOffsets", () => {
  it("tags each rendered text run with the source range it came from", () => {
    const source = "# Plan\n\nShip the **weekly** report.\n";
    const root = renderPreview(source);
    const runs = Array.from(root.querySelectorAll("span[data-md-start]")).map((span) =>
      source.slice(
        Number(span.getAttribute("data-md-start")),
        Number(span.getAttribute("data-md-end")),
      ),
    );
    expect(runs).toEqual(["Plan", "Ship the ", "weekly", " report."]);
  });
});

describe("previewSelectionToSource", () => {
  it("maps a plain selection to the same characters in the source", () => {
    const source = "Intro.\n\nThe quick brown fox.\n";
    const root = renderPreview(source);
    const mapped = previewSelectionToSource(selectRendered(root, "quick brown"), root, source);
    expect(mapped).toEqual({
      start_index: source.indexOf("quick"),
      end_index: source.indexOf("quick") + "quick brown".length,
      anchor_content: "quick brown",
    });
  });

  it("spans formatting, keeping the Markdown markers inside the range", () => {
    const source = "Ship the **weekly** report today.\n";
    const root = renderPreview(source);
    const mapped = previewSelectionToSource(
      selectRendered(root, "the weekly report"),
      root,
      source,
    );
    expect(mapped?.anchor_content).toBe("the **weekly** report");
  });

  it("finds text inside inline code past its backticks", () => {
    const source = "Run `npm test` first.\n";
    const root = renderPreview(source);
    const mapped = previewSelectionToSource(selectRendered(root, "npm"), root, source);
    expect(mapped?.anchor_content).toBe("npm");
  });

  it("snaps to the source run when the rendered text differs from it", () => {
    const source = "Tom &amp; Jerry\n";
    const root = renderPreview(source);
    const mapped = previewSelectionToSource(selectRendered(root, "&"), root, source);
    expect(mapped?.anchor_content).toBe("Tom &amp; Jerry");
  });

  it("returns null when nothing mappable is selected", () => {
    const root = renderPreview("Text.\n");
    const range = document.createRange();
    range.selectNodeContents(document.createElement("div"));
    expect(previewSelectionToSource(range, root, "Text.\n")).toBeNull();
  });
});

describe("previewCaretToSource", () => {
  it("gives the source offset under a caret", () => {
    const source = "Alpha **beta** gamma\n";
    const root = renderPreview(source);
    const beta = selectRendered(root, "beta");
    expect(previewCaretToSource(beta.startContainer, beta.startOffset + 2, root, source)).toBe(
      source.indexOf("beta") + 2,
    );
  });
});

describe("sourceRangeToPreviewRanges", () => {
  it("paints a saved comment's source range back onto the rendered text", () => {
    const source = "Ship the **weekly** report today.\n";
    const root = renderPreview(source);
    const start = source.indexOf("the");
    const end = source.indexOf(" today");
    const painted = sourceRangeToPreviewRanges(previewRuns(root, source), start, end)
      .map((range) => range.toString())
      .join("");
    expect(painted).toBe("the weekly report");
  });
});
