import { describe, expect, it } from "vitest";
import { isFenceClosed, rehypeMarkUnclosedFences } from "./unclosedFences";

describe("isFenceClosed", () => {
  it.each([
    ["a backtick fence", "```vega-lite\n{}\n```", true],
    ["a tilde fence", "~~~vega-lite\n{}\n~~~", true],
    ["a longer closing fence", "```\n{}\n`````", true],
    ["a closing fence with trailing spaces and CRLF", "```\r\n{}\r\n```  \r\n", true],
    ["an indented closing fence", "```\n   {}\n   ```", true],
    ["indented code, which has no fence", "    {}", true],
    ["a fence with no closing line", "```vega-lite\n{}", false],
    ["an opener alone", "```vega-lite", false],
    ["a shorter closing run", "````\n{}\n```", false],
    ["the other fence character", "~~~\n{}\n```", false],
    ["a closing run followed by text", "```\n{}\n``` x", false],
    ["a closer indented 4 spaces, which is content", "```\n{}\n    ```", false],
    ["a tab-indented closer, which is content", "```\n{}\n\t```", false],
    ["a blockquoted line inside a top-level fence", "```\n{}\n> ```", false],
  ])("%s → %s", (_label, source, closed) => {
    expect(isFenceClosed(source)).toBe(closed);
  });

  it.each([
    ["a blockquote", "> ", "```\n> {}\n> ```", true],
    ["a nested blockquote", "> > ", "```\n> > {}\n> > ```", true],
    ["a blockquote the closer left", "> ", "```\n> {}\n```", false],
    ["a list item", "1. ", "```\n   {}\n   ```", true],
    ["a list item, closer past its indent", "1. ", "```\n   {}\n       ```", false],
  ])("inside %s", (_label, prefix, source, closed) => {
    expect(isFenceClosed(source, prefix)).toBe(closed);
  });
});

describe("rehypeMarkUnclosedFences", () => {
  const markdown = "```js\na\n```\n\n```vega-lite\n{}\n";
  const pre = (start: number, end: number) => ({
    type: "element",
    tagName: "pre",
    properties: {} as Record<string, unknown>,
    position: { start: { offset: start }, end: { offset: end } },
  });

  it("marks only the pre whose fence never closed", () => {
    const closed = pre(0, 11);
    const unclosed = pre(13, markdown.length);
    const tree = { type: "root", children: [closed, unclosed] };

    rehypeMarkUnclosedFences()(tree, { value: markdown });

    expect(closed.properties).toEqual({});
    expect(unclosed.properties).toEqual({ dataUnclosedFence: true });
  });

  it("leaves the tree alone without positions or source text", () => {
    const unpositioned = { type: "element", tagName: "pre", properties: {} };
    const unsourced = pre(13, markdown.length);

    rehypeMarkUnclosedFences()({ type: "root", children: [unpositioned] }, { value: markdown });
    rehypeMarkUnclosedFences()({ type: "root", children: [unsourced] }, {});

    expect(unpositioned.properties).toEqual({});
    expect(unsourced.properties).toEqual({});
  });

  it("reads the list marker before a fence as its container", () => {
    // Indented 2 past the list item's content, which only the marker makes valid.
    const listed = "1. ```vega-lite\n   {}\n     ```\n";
    const node = pre(3, listed.trimEnd().length);

    rehypeMarkUnclosedFences()({ type: "root", children: [node] }, { value: listed });

    expect(node.properties).toEqual({});
  });
});
