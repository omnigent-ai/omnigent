import { describe, expect, it } from "vitest";

import { markdownLines } from "@/lib/pullRequestStacks/markdownLines";

describe("markdownLines", () => {
  it("keeps exact offsets across LF, CRLF and a last line without a break", () => {
    const body = "one\r\ntwo\nthree";
    const lines = markdownLines(body);
    expect(lines.map((line) => line.text)).toEqual(["one", "two", "three"]);
    expect(lines.map((line) => body.slice(line.start, line.end))).toEqual([
      "one\r\n",
      "two\n",
      "three",
    ]);
  });

  it("marks fenced lines through the closing fence, and quoted lines as Markdown reads them", () => {
    const lines = markdownLines("a\n```md\n## inside\n```\n> quoted\nb\n\nc\n");
    expect(lines.map((line) => [line.text, line.fenced, line.quoted])).toEqual([
      ["a", false, false],
      ["```md", true, false],
      ["## inside", true, false],
      ["```", true, false],
      ["> quoted", false, true],
      // A line right after a quote's paragraph continues it.
      ["b", false, true],
      ["", false, false],
      ["c", false, false],
    ]);
  });

  it("closes a fence only with the same character, at least as long, and no info string", () => {
    const lines = markdownLines("````\n```\n~~~~\n```` js\n````\nafter\n");
    expect(lines.map((line) => line.fenced)).toEqual([true, true, true, true, true, false]);
  });

  it("reads a line of inline code in triple backticks as prose, so what follows stays unfenced", () => {
    const lines = markdownLines("```inline```\n## Stack\n~~~ a`b\nx\n~~~\n");
    expect(lines.map((line) => line.fenced)).toEqual([false, false, true, true, true]);
  });

  it("closes a fence only with spaces or tabs after it, never another space character", () => {
    const lines = markdownLines("```\n```\u00a0\ninside\n``` \t\nafter\n");
    expect(lines.map((line) => line.fenced)).toEqual([true, true, true, true, false]);
  });

  it("ends a list item's fence where the item ends, and reads an outdented fence as a new one", () => {
    const outdented = markdownLines("- Example\n  ```\n  sample\n```\n## Stack\n```\nafter\n");
    expect(outdented.map((line) => line.fenced)).toEqual([
      false,
      true,
      true,
      true,
      true,
      true,
      false,
    ]);
    const ended = markdownLines("- Example\n  ```\n  sample\n## Stack\n");
    expect(ended.map((line) => line.fenced)).toEqual([false, true, true, false]);
  });

  it("reads a fence indented less than the list item's content as one outside the item", () => {
    const lines = markdownLines("- Example\n   ```\n   sample\n ```\n## Stack\n");
    expect(lines.map((line) => line.fenced)).toEqual([false, true, true, true, true]);
  });

  it("keeps a list item's fence open through lines indented as far as the item's content", () => {
    const lines = markdownLines("- Example\n   ```\n  sample\n   ```\nafter\n");
    expect(lines.map((line) => line.fenced)).toEqual([false, true, true, true, false]);
  });

  it("counts a tab after a list marker to its tab stop, so a fence past it stays in the item", () => {
    const lines = markdownLines("-\tExample\n   ```\n## Stack\n```\n");
    expect(lines.map((line) => line.fenced)).toEqual([false, true, true, true]);
  });

  it("marks a block of raw HTML, such as a comment, like code", () => {
    const lines = markdownLines("<!--\n## Stack\n-->\nafter\n");
    expect(lines.map((line) => line.fenced)).toEqual([true, true, true, false]);
  });

  it("leaves an unclosed fence open to the end", () => {
    expect(markdownLines("~~~\nx\n").map((line) => line.fenced)).toEqual([true, true]);
  });
});
