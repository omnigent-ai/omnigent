// Commenting on the rendered Markdown preview, driven through CodeViewer's real
// react-markdown pipeline. jsdom has no layout or CSS Custom Highlight API, so
// rects are stubbed and a fake highlight registry records what would be painted.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { useFileContent } from "@/hooks/useFileContent";
import type { Comment } from "@/hooks/useComments";
import * as permissions from "@/hooks/usePermissions";
import { CodeViewer } from "./CodeViewer";
import type { ActiveSelection } from "./codeViewerHelpers";
import { alignTextToSource, rehypeSourceOffsets } from "./markdownPreviewSource";

vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn() }));

const painted = new Map<string, string[]>();

class FakeHighlight {
  ranges: Range[] = [];
  add(range: Range) {
    this.ranges.push(range);
  }
}

beforeEach(() => {
  vi.mocked(permissions.useCanEdit).mockReturnValue(true);
  painted.clear();
  vi.stubGlobal("Highlight", FakeHighlight);
  vi.stubGlobal("CSS", {
    highlights: {
      set: (name: string, highlight: FakeHighlight) =>
        painted.set(
          name,
          highlight.ranges.map((range) => range.toString()),
        ),
      delete: (name: string) => painted.delete(name),
    },
  });
  const rect = { top: 40, bottom: 60, left: 10, right: 50, width: 40, height: 20 } as DOMRect;
  Range.prototype.getClientRects = vi.fn(() => [rect] as unknown as DOMRectList);
  Range.prototype.getBoundingClientRect = vi.fn(() => rect);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.getSelection()?.removeAllRanges();
});

function fileQuery(content: string): ReturnType<typeof useFileContent> {
  return {
    data: { content, encoding: "utf-8", truncated: false },
    isLoading: false,
    isError: false,
    isSuccess: true,
    error: null,
  } as unknown as ReturnType<typeof useFileContent>;
}

function comment(id: string, content: string, quote: string): Comment {
  const start = content.indexOf(quote);
  return {
    id,
    conversation_id: "conv_1",
    path: "notes.md",
    start_index: start,
    end_index: start + quote.length,
    body: "Looks off",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: quote,
    created_by: null,
  };
}

function renderPreview(
  content: string,
  {
    comments = [],
    activeSelection = null,
    onSetActiveSelection = () => {},
    markdownSourceFallback = false,
    path = "notes.md",
  }: {
    comments?: Comment[];
    activeSelection?: ActiveSelection | null;
    onSetActiveSelection?: (selection: ActiveSelection | null) => void;
    markdownSourceFallback?: boolean;
    path?: string;
  } = {},
) {
  return render(
    <CodeViewer
      markdownSourceFallback={markdownSourceFallback}
      conversationId="conv_1"
      path={path}
      fileQuery={fileQuery(content)}
      comments={comments}
      activeSelection={activeSelection}
      onSetActiveSelection={onSetActiveSelection}
      panelOpen
      searchOpen={false}
      setSearchOpen={() => {}}
      searchInputRef={{ current: null }}
      viewMode="preview"
      onRequestEditMode={() => {}}
    />,
  );
}

function previewRoot(): HTMLElement {
  const root = document.querySelector<HTMLElement>(".markdown-preview");
  if (!root) throw new Error("preview not rendered");
  return root;
}

/** DOM position of rendered-text offset `index` in the preview. */
function domPoint(root: HTMLElement, index: number): [Text, number] {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let seen = 0;
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    if (index <= seen + node.data.length) return [node, index - seen];
    seen += node.data.length;
  }
  throw new Error(`offset ${index} is past the preview text`);
}

/** Select `text` as rendered (DOM text order), then release the mouse. */
function selectRendered(text: string) {
  const root = previewRoot();
  const index = root.textContent?.indexOf(text) ?? -1;
  if (index === -1) throw new Error(`"${text}" is not rendered`);
  const range = document.createRange();
  range.setStart(...domPoint(root, index));
  range.setEnd(...domPoint(root, index + text.length));
  const selection = window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
  fireEvent.mouseDown(root);
  fireEvent.mouseUp(root);
}

function clickAt(text: string) {
  const root = previewRoot();
  const index = root.textContent?.indexOf(text) ?? -1;
  const range = document.createRange();
  range.setStart(...domPoint(root, index + 1));
  range.collapse(true);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  fireEvent.mouseDown(root);
  fireEvent.mouseUp(root);
}

describe("commenting on a selection in the Markdown preview", () => {
  it.each([
    ["a plain word", "Intro with Review word.\n", "Review", "Review", "Review"],
    ["a word split by bold markup", "A wo**rd** here\n", "word", "word", "wo**rd"],
    ["a heading", "# Title here\n", "Title", "Title", "Title"],
    ["a tight list item", "- first\n- second item\n", "second", "second", "second"],
    [
      "a table cell",
      "| a | b |\n|---|---|\n| cell one | two |\n",
      "cell one",
      "cell one",
      "cell one",
    ],
    ["a fenced code line", "```python\nprint('hi')\n```\n", "print", "print", "print"],
    ["inline code", "Run `just lint` now\n", "just lint", "just lint", "just lint"],
    ["escaped markup", "a \\*star\\* b\n", "*star*", "*star*", "*star\\*"],
    ["a decoded entity", "Tom &amp; Jerry\n", "Tom & Jerry", "Tom & Jerry", "Tom &amp; Jerry"],
    ["text after an emoji shortcode", "Ship it :rocket: now\n", "now", "now", "now"],
    ["a quote's continuation line", "> alpha\n> beta\n", "pha\nbe", "pha\nbe", "pha\n> be"],
    [
      "two paragraphs",
      "First para.\n\nSecond para.\n",
      "para.\nSecond",
      "para.\nSecond",
      "para.\n\nSecond",
    ],
    [
      "padded whitespace",
      "Keep this phrase intact\n",
      " this phrase ",
      "this phrase",
      "this phrase",
    ],
    ["raw HTML text", "Press <kbd>Ctrl</kbd> twice\n", "Ctrl", "Ctrl", "Ctrl"],
    [
      "an alert body",
      "> [!NOTE]\n> Mind the gap\n",
      "Mind the gap",
      "Mind the gap",
      "Mind the gap",
    ],
    [
      "link text",
      "See [the guide](https://example.com) here\n",
      "the guide",
      "the guide",
      "the guide",
    ],
    ["a nested list item", "- outer\n  - inner leaf\n", "inner leaf", "inner leaf", "inner leaf"],
    ["text beside math", "Area $$x^2$$ grows fast\n", "grows", "grows", "grows"],
    ["struck text", "Drop ~~old plan~~ today\n", "old plan", "old plan", "old plan"],
  ])("anchors %s to its raw-source range", (_label, content, selected, quote, sourceSlice) => {
    const onSetActiveSelection = vi.fn();
    renderPreview(content, { onSetActiveSelection });

    selectRendered(selected);
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));

    expect(onSetActiveSelection).toHaveBeenCalledOnce();
    const anchor = onSetActiveSelection.mock.calls[0][0] as ActiveSelection;
    expect(anchor.anchor_content).toBe(quote);
    expect(content.slice(anchor.start_index, anchor.end_index)).toBe(sourceSlice);
  });

  it("anchors the selected copy of repeated text", () => {
    const content = "Repeat one.\n\nThen repeat again: Repeat two.\n";
    const onSetActiveSelection = vi.fn();
    renderPreview(content, { onSetActiveSelection });

    selectRendered("Repeat two");
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));

    const anchor = onSetActiveSelection.mock.calls[0][0] as ActiveSelection;
    expect(anchor.start_index).toBe(content.lastIndexOf("Repeat two"));
  });

  it("offers no button to read-only viewers", () => {
    vi.mocked(permissions.useCanEdit).mockReturnValue(false);
    renderPreview("Intro with Review word.\n");

    selectRendered("Review");

    expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();
  });

  it("replaces the switch-to-edit hint for regular Markdown", () => {
    renderPreview("# doc\n");
    expect(screen.queryByRole("button", { name: /switch to edit mode/i })).toBeNull();
  });

  it("keeps large Markdown previews on the hint, without source tagging", () => {
    renderPreview("Intro with Review word.\n", { markdownSourceFallback: true });

    expect(screen.getByRole("button", { name: /switch to edit mode/i })).toBeDefined();
    expect(previewRoot().querySelector("[data-source-start]")).toBeNull();
    selectRendered("Review");
    expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();
  });

  it("leaves notebook previews without a comment layer", () => {
    const notebook = JSON.stringify({
      nbformat: 4,
      cells: [{ cell_type: "markdown", metadata: {}, source: ["Notebook Review text\n"] }],
    });
    renderPreview(notebook, { path: "analysis.ipynb" });

    expect(document.querySelector("[data-source-start]")).toBeNull();
  });
});

describe("saved comments in the Markdown preview", () => {
  const content = "Intro with **Review** word and a second note.\n";

  it("highlights each comment and the active one separately", () => {
    const review = comment("c1", content, "Review");
    const note = comment("c2", content, "second note");
    renderPreview(content, {
      comments: [review, note],
      activeSelection: {
        start_index: note.start_index,
        end_index: note.end_index,
        anchor_content: "second note",
        comment_id: "c2",
      },
    });

    expect(painted.get("md-preview-comment")).toEqual(["Review"]);
    expect(painted.get("md-preview-comment-active")).toEqual(["second note"]);
    expect(painted.get("md-preview-comment-pending")).toEqual([]);
  });

  it("highlights the range of a draft that is still being written", () => {
    const start = content.indexOf("word");
    renderPreview(content, {
      activeSelection: { start_index: start, end_index: start + 4, anchor_content: "word" },
    });

    expect(painted.get("md-preview-comment-pending")).toEqual(["word"]);
  });

  it("activates a comment when its text is clicked", () => {
    const review = comment("c1", content, "Review");
    const onSetActiveSelection = vi.fn();
    renderPreview(content, { comments: [review], onSetActiveSelection });

    clickAt("Review");

    expect(onSetActiveSelection).toHaveBeenCalledWith({
      start_index: review.start_index,
      end_index: review.end_index,
      anchor_content: "Review",
      comment_id: "c1",
    });
  });

  it("clears the active comment when other text is clicked", () => {
    const review = comment("c1", content, "Review");
    const onSetActiveSelection = vi.fn();
    renderPreview(content, {
      comments: [review],
      activeSelection: { ...review, anchor_content: "Review", comment_id: "c1" },
      onSetActiveSelection,
    });

    clickAt("second");

    expect(onSetActiveSelection).toHaveBeenCalledWith(null);
  });

  it("removes its highlights when the preview closes", async () => {
    const view = renderPreview(content, { comments: [comment("c1", content, "Review")] });
    expect(painted.get("md-preview-comment")).toEqual(["Review"]);

    await act(async () => view.unmount());

    expect(painted.get("md-preview-comment")).toEqual([]);
  });
});

describe("rehypeSourceOffsets", () => {
  const position = (start: number, end: number) => ({
    start: { offset: start },
    end: { offset: end },
  });

  it("wraps positioned text but leaves math, Mermaid and blank text alone", () => {
    const tree = {
      type: "root",
      children: [
        {
          type: "element",
          tagName: "p",
          children: [
            { type: "text", value: "Prose", position: position(0, 5) },
            { type: "text", value: "\n" },
            {
              type: "element",
              tagName: "code",
              properties: { className: ["language-math", "math-inline"] },
              children: [{ type: "text", value: "x^2", position: position(8, 11) }],
            },
          ],
        },
        {
          type: "element",
          tagName: "pre",
          children: [
            {
              type: "element",
              tagName: "code",
              properties: { className: ["language-Mermaid"] },
              children: [{ type: "text", value: "graph TD" }],
              position: position(20, 40),
            },
          ],
        },
      ],
    };

    rehypeSourceOffsets()(tree as never);

    const paragraph = tree.children[0].children;
    expect(paragraph[0]).toMatchObject({
      tagName: "span",
      properties: { dataSourceStart: 0, dataSourceEnd: 5, dataSourceBlock: 1 },
    });
    expect(paragraph[1]).toEqual({ type: "text", value: "\n" });
    expect(paragraph[2].children?.[0]).toMatchObject({ type: "text", value: "x^2" });
    expect(tree.children[1].children[0].children?.[0]).toEqual({
      type: "text",
      value: "graph TD",
    });
  });
});

describe("alignTextToSource", () => {
  it("maps verbatim text one to one", () => {
    expect(alignTextToSource("abc", "xxabcxx", 2, 5)).toEqual([2, 3, 4]);
  });

  it("skips markers the renderer dropped", () => {
    const source = "> one\n> two";
    expect(alignTextToSource("one\ntwo", source, 2, source.length)).toEqual([2, 3, 4, 5, 8, 9, 10]);
  });

  it("keeps characters after an unmatched glyph aligned", () => {
    const source = "a :rocket: b";
    const offsets = alignTextToSource("a 🚀 b", source, 0, source.length);
    expect(offsets.at(-1)).toBe(source.length - 1);
    expect(offsets.at(-2)).toBe(source.length - 2);
  });
});
