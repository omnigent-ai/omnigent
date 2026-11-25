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

  it("marks fenced lines through the closing fence, and quoted lines", () => {
    const lines = markdownLines("a\n```md\n## inside\n```\n> quoted\nb\n");
    expect(lines.map((line) => [line.text, line.fenced, line.quoted])).toEqual([
      ["a", false, false],
      ["```md", true, false],
      ["## inside", true, false],
      ["```", true, false],
      ["> quoted", false, true],
      ["b", false, false],
    ]);
  });

  it("closes a fence only with the same character, at least as long, and no info string", () => {
    const lines = markdownLines("````\n```\n~~~~\n```` js\n````\nafter\n");
    expect(lines.map((line) => line.fenced)).toEqual([true, true, true, true, true, false]);
  });

  it("leaves an unclosed fence open to the end", () => {
    expect(markdownLines("~~~\nx\n").map((line) => line.fenced)).toEqual([true, true]);
  });
});
