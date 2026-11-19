// Commenting on the rendered Markdown preview, driven through CodeViewer's real
// react-markdown pipeline. jsdom has no layout or CSS Custom Highlight API, so
// rects are stubbed and a fake highlight registry records what would be painted.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { useFileContent } from "@/hooks/useFileContent";
import type { Comment } from "@/hooks/useComments";
import * as permissions from "@/hooks/usePermissions";
import { CodeViewer } from "./CodeViewer";
import type { ActiveSelection } from "./codeViewerHelpers";
import {
  alignTextToSource,
  previewPointToSource,
  rehypeSourceOffsets,
} from "./markdownPreviewSource";

vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn() }));

const painted = new Map<string, string[]>();

class FakeHighlight {
  ranges: Range[] = [];
  add(range: Range) {
    this.ranges.push(range);
  }
}

// jsdom has no Range layout or Element.scrollBy, so these are stubbed per test.
const STUBBED_LAYOUT = [
  [Range.prototype, "getClientRects"],
  [Range.prototype, "getBoundingClientRect"],
  [Element.prototype, "scrollBy"],
] as const;
const originalLayout = STUBBED_LAYOUT.map(
  ([target, name]) => [target, name, Object.getOwnPropertyDescriptor(target, name)] as const,
);

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
  Element.prototype.scrollBy = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // Restore jsdom's methods, removing the stubs where it had none.
  for (const [target, name, descriptor] of originalLayout) {
    if (descriptor) Object.defineProperty(target, name, descriptor);
    else Reflect.deleteProperty(target, name);
  }
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

/** Select `text` (its last copy if `last`) as rendered, then release the mouse. */
function selectRendered(text: string, { last = false } = {}) {
  const root = previewRoot();
  const rendered = root.textContent ?? "";
  const index = last ? rendered.lastIndexOf(text) : rendered.indexOf(text);
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

/** Click with the caret `offset` characters into the rendered `text`. */
function clickAt(text: string, offset = 1) {
  const root = previewRoot();
  const index = root.textContent?.indexOf(text) ?? -1;
  const range = document.createRange();
  range.setStart(...domPoint(root, index + offset));
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
    ["escaped markup", "a \\*star\\* b\n", "*star*", "*star*", "\\*star\\*"],
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
    ["bold words split by a space", "**one** **two**\n", "one two", "one two", "one** **two"],
    [
      "bold words split by a soft break",
      "**one**\n**two**\n",
      "one\ntwo",
      "one\ntwo",
      "one**\n**two",
    ],
    ["a named entity alone", "Made &copy; here\n", "©", "©", "&copy;"],
    ["an entity in inline code", "Write `&amp;` here\n", "&amp;", "&amp;", "&amp;"],
    ["an escaped entity", "Write &amp;amp; here\n", "&amp;", "&amp;", "&amp;amp;"],
    ["an emoji", "Ship it :rocket: now\n", "🚀", "🚀", ":rocket:"],
    [
      "a deeply indented continuation line",
      `- item one\n${" ".repeat(30)}continues here\n`,
      "one\ncontinues",
      "one\ncontinues",
      `one\n${" ".repeat(30)}continues`,
    ],
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

  it("anchors text after an entity past the whole reference", () => {
    const content = "&amp;am\n";
    const onSetActiveSelection = vi.fn();
    renderPreview(content, { onSetActiveSelection });

    selectRendered("am");
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));

    const anchor = onSetActiveSelection.mock.calls[0][0] as ActiveSelection;
    expect([anchor.start_index, anchor.end_index]).toEqual([5, 7]);
  });

  it("offers the button when a drag is released outside the preview", () => {
    renderPreview("Intro with Review word.\n");
    const root = previewRoot();
    const index = root.textContent?.indexOf("Review") ?? -1;
    const range = document.createRange();
    range.setStart(...domPoint(root, index));
    range.setEnd(...domPoint(root, index + "Review".length));

    fireEvent.mouseDown(root);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    fireEvent.mouseUp(document.body);

    expect(screen.getByRole("button", { name: "Add comment" })).toBeDefined();
  });

  it("drops the button when the selection collapses", async () => {
    renderPreview("Intro with Review word.\n");
    selectRendered("Review");
    expect(screen.getByRole("button", { name: "Add comment" })).toBeDefined();

    window.getSelection()!.collapseToEnd();
    fireEvent(document, new Event("selectionchange"));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull());
  });

  it("anchors the selected copy of repeated text", () => {
    const content = "Same word, then **Same** word.\n";
    const onSetActiveSelection = vi.fn();
    renderPreview(content, { onSetActiveSelection });

    selectRendered("Same word", { last: true });
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));

    const anchor = onSetActiveSelection.mock.calls[0][0] as ActiveSelection;
    const start = content.lastIndexOf("Same");
    expect([anchor.start_index, anchor.end_index]).toEqual([
      start,
      content.lastIndexOf("word") + 4,
    ]);
  });

  it("offers no button for a selection across rendered math", () => {
    renderPreview("Area $$x^2$$ grows fast\n");
    const root = previewRoot();
    const rendered = root.textContent ?? "";
    const range = document.createRange();
    range.setStart(...domPoint(root, rendered.indexOf("Area")));
    range.setEnd(...domPoint(root, rendered.indexOf("grows") + "grows".length));
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    fireEvent.mouseDown(root);
    fireEvent.mouseUp(root);

    expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();
  });

  it("matches every rendered span of a mixed document to its source", () => {
    const content = [
      "# Title :rocket: &copy;",
      "",
      "Text with \\*stars\\*, a footnote[^1], `code &amp;` and Tom &amp; Jerry.",
      "",
      "> Quoted line",
      "> and its continuation",
      "",
      "- [ ] task item",
      "- item with",
      "      deep continuation",
      "",
      "  ```js",
      "  const indented = 1;",
      "  ```",
      "",
      "| a | b \\| c |",
      "|---|---|",
      "| cell | two |",
      "",
      "> [!NOTE]",
      "> Mind the gap",
      "",
      "<details><summary>More</summary>",
      "",
      "Hidden body",
      "",
      "</details>",
      "",
      "Line one  ",
      "line two",
      "",
      "[^1]: The note.",
      "",
    ].join("\n");
    renderPreview(content);

    const unmatched = [...previewRoot().querySelectorAll("span[data-source-start]")]
      .filter((span) => previewPointToSource(span.firstChild!, 0, content) === null)
      .map((span) => span.textContent);
    expect(unmatched).toEqual([]);
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

  it("does not activate a comment from a click just past its last character", () => {
    const onSetActiveSelection = vi.fn();
    renderPreview(content, { comments: [comment("c1", content, "Review")], onSetActiveSelection });

    clickAt("Review", "Review".length);

    expect(onSetActiveSelection).not.toHaveBeenCalled();
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

  it("highlights an editor comment placed after an entity", () => {
    const entitySource = "&amp;am and more\n";
    const after = { ...comment("c1", entitySource, "am"), start_index: 5, end_index: 7 };
    renderPreview(entitySource, { comments: [after] });

    expect(painted.get("md-preview-comment")).toEqual(["am"]);
  });

  it("scrolls the chosen comment's own text into view", () => {
    const tall = `${"Filler words. ".repeat(40)}Review this.\n`;
    const review = comment("c1", tall, "Review");
    const inView = { top: 0, bottom: 100, left: 0, right: 100, width: 100, height: 100 };
    const layout = vi
      .spyOn(Element.prototype, "getBoundingClientRect")
      .mockReturnValue(inView as DOMRect);
    const below = { top: 300, bottom: 320, left: 10, right: 50, width: 40, height: 20 };
    Range.prototype.getBoundingClientRect = vi.fn(() => below as DOMRect);
    try {
      renderPreview(tall, {
        comments: [review],
        activeSelection: { ...review, anchor_content: "Review", comment_id: "c1" },
      });

      expect(previewRoot().scrollBy).toHaveBeenCalledWith({ top: 260, behavior: "smooth" });
    } finally {
      layout.mockRestore();
    }
  });

  it("removes its highlights when the preview closes", async () => {
    const view = renderPreview(content, { comments: [comment("c1", content, "Review")] });
    expect(painted.get("md-preview-comment")).toEqual(["Review"]);

    await act(async () => view.unmount());

    expect(painted.get("md-preview-comment")).toEqual([]);
  });

  it("keeps another preview's highlights when one closes", async () => {
    const first = renderPreview(content, { comments: [comment("c1", content, "Review")] });
    renderPreview(content, { comments: [comment("c2", content, "second note")] });
    expect(painted.get("md-preview-comment")).toEqual(["Review", "second note"]);

    await act(async () => first.unmount());

    expect(painted.get("md-preview-comment")).toEqual(["second note"]);
  });
});

describe("rehypeSourceOffsets", () => {
  const position = (start: number, end: number) => ({
    start: { offset: start },
    end: { offset: end },
  });

  it("places repeated unpositioned alert children in order", () => {
    const source = "> [!NOTE]\n> Same\n>\n> Same\n\nAfter\n";
    const paragraph = () => ({
      type: "element",
      tagName: "p",
      children: [{ type: "text", value: "Same" }],
    });
    const after = source.indexOf("After");
    const tree = {
      type: "root",
      children: [
        {
          type: "element",
          tagName: "div",
          properties: { className: ["markdown-alert"] },
          children: [paragraph(), paragraph()],
        },
        {
          type: "element",
          tagName: "p",
          position: position(after, after + 5),
          children: [{ type: "text", value: "After", position: position(after, after + 5) }],
        },
      ],
    };

    rehypeSourceOffsets()(tree as never, { value: source });

    const alertChildren = tree.children[0].children as { children: unknown[] }[];
    const starts = alertChildren.map(
      (child) => (child.children[0] as { properties?: { dataSourceStart?: number } }).properties,
    );
    expect(starts).toEqual([
      expect.objectContaining({ dataSourceStart: source.indexOf("Same") }),
      expect.objectContaining({ dataSourceStart: source.lastIndexOf("Same") }),
    ]);
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
  it("spans a decoded entity over its whole reference", () => {
    expect(alignTextToSource("© x", "&copy; x", 0, 8)).toEqual({
      starts: [0, 6, 7],
      ends: [6, 7, 8],
    });
  });

  it("maps verbatim text one to one", () => {
    expect(alignTextToSource("abc", "xxabcxx", 2, 5)).toEqual({
      starts: [2, 3, 4],
      ends: [3, 4, 5],
    });
  });

  it("skips markers the renderer dropped", () => {
    const source = "> one\n> two";
    expect(alignTextToSource("one\ntwo", source, 2, source.length)?.starts).toEqual([
      2, 3, 4, 5, 8, 9, 10,
    ]);
  });

  it("skips an indent of any width", () => {
    const source = `one\n${" ".repeat(30)}two`;
    expect(alignTextToSource("one\ntwo", source, 0, source.length)?.starts).toEqual([
      0, 1, 2, 3, 34, 35, 36,
    ]);
  });

  it("spans an emoji glyph over its shortcode", () => {
    const source = "a :rocket: b";
    expect(alignTextToSource("a 🚀 b", source, 0, source.length)).toEqual({
      starts: [0, 1, 2, 2, 10, 11],
      ends: [1, 2, 10, 10, 11, 12],
    });
  });

  it("decodes entities in prose but not in code", () => {
    const source = "&amp;amp;\n  &amp;";
    expect(alignTextToSource("&amp;", source, 0, 9)?.starts).toEqual([0, 5, 6, 7, 8]);
    expect(alignTextToSource("&amp;amp;\n&amp;", source, 0, source.length, true)?.starts).toEqual([
      0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16,
    ]);
    expect(alignTextToSource("&\n&", source, 0, source.length, true)).toBeNull();
  });

  it("declines text it cannot match to its source", () => {
    const source = "a :rocket: b";
    expect(alignTextToSource("a ? b", source, 0, source.length)).toBeNull();
  });
});
