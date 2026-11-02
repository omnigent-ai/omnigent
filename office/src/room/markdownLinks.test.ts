import { describe, expect, it } from "vitest";
import { isMarkdownFile, resolveMarkdownLink } from "./markdownLinks.ts";

describe("resolveMarkdownLink", () => {
  it("opens relative files of the room's folder from the current file's folder", () => {
    expect(resolveMarkdownLink("README.md", "docs/architecture.md")).toEqual({ kind: "file", path: "docs/architecture.md" });
    expect(resolveMarkdownLink("docs/qa/plan.md", "../architecture.md#gates")).toEqual({ kind: "file", path: "docs/architecture.md" });
    expect(resolveMarkdownLink("docs/plan.md", "./images/a%20b.png")).toEqual({ kind: "file", path: "docs/images/a b.png" });
    expect(resolveMarkdownLink("docs/plan.md", "/src/main.js")).toEqual({ kind: "file", path: "src/main.js" });
  });

  it("sends web links out, keeps anchors in the page, and blocks everything else", () => {
    expect(resolveMarkdownLink("README.md", "https://example.com")).toEqual({ kind: "external", href: "https://example.com" });
    expect(resolveMarkdownLink("README.md", "mailto:po@example.com")).toEqual({ kind: "external", href: "mailto:po@example.com" });
    expect(resolveMarkdownLink("README.md", "#launch")).toEqual({ kind: "anchor", id: "launch" });
    expect(resolveMarkdownLink("README.md", "javascript:alert(1)")).toEqual({ kind: "blocked" });
    expect(resolveMarkdownLink("README.md", "file:///etc/passwd")).toEqual({ kind: "blocked" });
    expect(resolveMarkdownLink("README.md", "../outside.md")).toEqual({ kind: "blocked" });
  });
});

describe("isMarkdownFile", () => {
  it("recognizes Markdown extensions", () => {
    expect(isMarkdownFile("docs/README.md")).toBe(true);
    expect(isMarkdownFile("notes.MARKDOWN")).toBe(true);
    expect(isMarkdownFile("main.js")).toBe(false);
  });
});
