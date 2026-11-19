import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MessageResponse } from "@/components/ai-elements/message";

import { selectionToMarkdown } from "./selectionMarkdown";

afterEach(cleanup);

/** Renders agent markdown the way the chat transcript does. */
async function renderMarkdown(markdown: string): Promise<HTMLElement> {
  const { container } = render(
    <MessageResponse className="chat-markdown" mode="static">
      {markdown}
    </MessageResponse>,
  );
  // Code highlighting resolves after the first paint.
  await act(async () => {});
  return container;
}

/** A range from the start of `startText` to the end of `endText`. */
function rangeBetween(root: Element, startText: string, endText: string): Range {
  const range = document.createRange();
  const start = textNodeContaining(root, startText);
  const end = textNodeContaining(root, endText);
  range.setStart(start, start.data.indexOf(startText));
  range.setEnd(end, end.data.indexOf(endText) + endText.length);
  return range;
}

function textNodeContaining(root: Element, text: string): Text {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if ((node as Text).data.includes(text)) return node as Text;
  }
  throw new Error(`no text node contains ${JSON.stringify(text)}`);
}

describe("selectionToMarkdown", () => {
  it("keeps bullets and nesting for a selection spanning a whole list", async () => {
    const root = await renderMarkdown("Intro **bold**\n\n- one\n- two\n  - nested\n\nAfter");
    const range = document.createRange();
    range.selectNodeContents(root);

    expect(selectionToMarkdown(range, root)).toBe(
      "Intro **bold**\n\n- one\n- two\n  - nested\n\nAfter",
    );
  });

  it("re-wraps items selected inside one list and keeps their numbering", async () => {
    const root = await renderMarkdown("1. first\n2. second\n3. third");

    expect(selectionToMarkdown(rangeBetween(root, "second", "third"), root)).toBe(
      "2. second\n3. third",
    );
  });

  it("keeps task-list checkboxes", async () => {
    const root = await renderMarkdown("- [x] done\n- [ ] todo");
    const range = rangeBetween(root, "done", "todo");
    range.setStartBefore(root.querySelector("input")!);

    expect(selectionToMarkdown(range, root)).toBe("- [x] done\n- [ ] todo");
  });

  it("keeps highlighted code lines and drops the code block's controls", async () => {
    const root = await renderMarkdown("- step\n\n```python\nprint(1)\nprint(2)\n```");
    await waitFor(() => expect(root.querySelector("pre code span")).not.toBeNull());
    const range = document.createRange();
    range.selectNodeContents(root);

    expect(selectionToMarkdown(range, root)).toBe("- step\n\n```python\nprint(1)\nprint(2)\n```");
  });

  it("leaves selections without a list item to the browser", async () => {
    const root = await renderMarkdown("Some **bold** text\n\n- item");

    expect(selectionToMarkdown(rangeBetween(root, "Some", "text"), root)).toBeNull();
    expect(selectionToMarkdown(rangeBetween(root, "it", "em"), root)).toBeNull();
  });

  it("ignores a selection reaching outside the rendered markdown", async () => {
    const root = await renderMarkdown("- item");
    const range = document.createRange();
    range.selectNodeContents(document.body);

    expect(selectionToMarkdown(range, root)).toBeNull();
  });
});
