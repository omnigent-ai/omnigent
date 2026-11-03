import { describe, expect, it } from "vitest";
import { clipboardHtmlToMarkdown } from "./pasteMarkdown";
import { markdownToHtml } from "./copyMarkdown";

describe("clipboardHtmlToMarkdown", () => {
  it("preserves links, headings, lists, quotes and code copied from a chat turn", () => {
    const source =
      "## Notes\n\n**Bold** and *italic*, ~~old~~ and [a link](https://example.com/docs).\n\n- First\n- Second\n\n> A quote\n\n`inline`\n\n```ts\nconst value = 1;\n```";
    const pasted = clipboardHtmlToMarkdown(markdownToHtml(source));
    expect(pasted).toBe(source);
  });

  it("keeps nested lists and tables from external HTML", () => {
    const pasted = clipboardHtmlToMarkdown(
      '<ul><li>Parent<ul><li>Child</li></ul></li></ul><table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td><a href="https://example.com">B</a></td></tr></table>',
    );
    expect(pasted).toContain("- Parent\n  - Child");
    expect(pasted).toMatch(/\| Name +\| Value +\|/);
    expect(pasted).toContain("[B](https://example.com)");
  });

  it("recognizes inline styles used by document editors", () => {
    expect(clipboardHtmlToMarkdown('<span style="font-weight: 700">Bold</span>')).toBe("**Bold**");
  });

  it("keeps code containing backticks inside its fence", () => {
    const source = "````md\n```ts\nexample\n```\n````";
    expect(clipboardHtmlToMarkdown(markdownToHtml(source))).toBe(source);
  });

  it.each([
    "",
    "<span>just text</span>",
    '<div><span style="color:red">**Markdown source**</span></div>',
  ])("leaves plain text wrappers alone: %s", (html) => {
    expect(clipboardHtmlToMarkdown(html)).toBeNull();
  });

  it("drops active content and unsafe links without executing or loading it", () => {
    const pasted = clipboardHtmlToMarkdown(
      '<p><strong>Keep</strong><script>alert(1)</script><style>body{display:none}</style><iframe src="https://example.com"></iframe><img src="https://example.com/tracker" onerror="alert(1)"> <a href="javascript:alert(1)">label</a></p>',
    );
    expect(pasted).toBe("**Keep** label");
  });
});
