// Local file URIs should reach FileViewer; unsafe forms stay for sanitize to block.

import { describe, expect, it } from "vitest";
import { rewriteFileUriLinks } from "./streamdown-security";

interface TestNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: TestNode[];
}

function anchor(href: string): TestNode {
  return { type: "element", tagName: "a", properties: { href }, children: [] };
}

function rewriteHref(href: string): Record<string, unknown> {
  const node = anchor(href);
  const tree: TestNode = {
    type: "root",
    children: [{ type: "element", tagName: "p", children: [node] }],
  };
  rewriteFileUriLinks()(tree);
  return node.properties ?? {};
}

describe("rewriteFileUriLinks", () => {
  it("rewrites a file:// URI to the absolute path it names", () => {
    expect(rewriteHref("file:///tmp/ws/report.md")).toEqual({ href: "/tmp/ws/report.md" });
  });

  it("rewrites regardless of scheme case", () => {
    expect(rewriteHref("FILE:///tmp/ws/report.md")).toEqual({ href: "/tmp/ws/report.md" });
  });

  it("treats a localhost authority as the local machine, per RFC 8089", () => {
    expect(rewriteHref("file://localhost/tmp/ws/report.md")).toEqual({
      href: "/tmp/ws/report.md",
    });
  });

  it("decodes percent-escapes so the path matches the file on disk", () => {
    expect(rewriteHref("file:///tmp/my%20notes/report.md")).toEqual({
      href: "/tmp/my notes/report.md",
    });
  });

  it.each([
    ["file://fileserver/share/report.md", "UNC host names another machine"],
    ["file:///tmp/report.md?raw=1", "query is not part of a filename"],
    ["file:///tmp/report.md#section", "fragment is not part of a filename"],
    ["file:////evil.com/x.md", "decoded // path would read as protocol-relative"],
    ["file:///tmp/a%3Fb.md", "decoded ? would split the rewritten href"],
    ["file:///tmp/a%23b.md", "decoded # would split the rewritten href"],
    ["file:///tmp/%E0%A4%A.md", "undecodable percent-escape"],
    ["file:///", "bare root names no file"],
  ])("leaves %s for sanitize to strip (%s)", (href) => {
    expect(rewriteHref(href)).toEqual({ href });
  });

  it.each([
    ["https://example.com/report.md", "http(s) URL"],
    ["/tmp/ws/report.md", "already a plain path"],
    ["mailto:someone@example.com", "other scheme"],
    ["#section-two", "in-page anchor"],
  ])("ignores %s (%s)", (href) => {
    expect(rewriteHref(href)).toEqual({ href });
  });

  it("rewrites every file:// link in the tree, not just the first", () => {
    const first = anchor("file:///ws/one.md");
    const second = anchor("file:///ws/two.md");
    const tree: TestNode = {
      type: "root",
      children: [
        { type: "element", tagName: "p", children: [first] },
        {
          type: "element",
          tagName: "ul",
          children: [{ type: "element", tagName: "li", children: [second] }],
        },
      ],
    };
    rewriteFileUriLinks()(tree);
    expect(first.properties?.href).toBe("/ws/one.md");
    expect(second.properties?.href).toBe("/ws/two.md");
  });

  it("ignores non-anchor elements and hrefless anchors", () => {
    const img: TestNode = { type: "element", tagName: "img", properties: { src: "x.png" } };
    const bare: TestNode = { type: "element", tagName: "a", properties: {} };
    rewriteFileUriLinks()({ type: "root", children: [img, bare] });
    expect(img.properties).toEqual({ src: "x.png" });
    expect(bare.properties).toEqual({});
  });
});
