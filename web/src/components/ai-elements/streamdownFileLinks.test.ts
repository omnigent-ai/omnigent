// Agents link files they just wrote. Before the marking pass, the harden step
// either kept such a link as a real anchor (clicking it navigated the app origin
// and 404'd), or stripped the href and appended " [blocked]", which read as
// though the app had censored it. These cases pin which hrefs get handed to the
// FileViewer renderer and which are still left to harden untouched.

import { describe, expect, it } from "vitest";
import { markWorkspaceFileLinks, WORKSPACE_FILE_LINK_ATTR } from "./streamdown-security";

interface TestNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: TestNode[];
}

function anchor(href: string): TestNode {
  return { type: "element", tagName: "a", properties: { href }, children: [] };
}

/** Runs the pass over a one-anchor tree and returns that anchor's properties. */
function markHref(href: string): Record<string, unknown> {
  const node = anchor(href);
  const tree: TestNode = {
    type: "root",
    children: [{ type: "element", tagName: "p", children: [node] }],
  };
  markWorkspaceFileLinks()(tree);
  return node.properties ?? {};
}

/**
 * A handed-over link keeps the original path on the data attribute and parks its
 * href on an inert fragment, so no click can navigate. The exact fragment is an
 * implementation detail; that it *is* a fragment is not.
 */
function expectHandedOver(properties: Record<string, unknown>, path: string): void {
  expect(properties[WORKSPACE_FILE_LINK_ATTR]).toBe(path);
  expect(properties.href).toMatch(/^#./);
}

describe("markWorkspaceFileLinks", () => {
  it("hands an absolute workspace path to the FileViewer renderer", () => {
    // Rendered as a live anchor before this pass, so a click hit the server.
    const path = "/Users/dev/Projects/app/services/gateway/docs/migration-proposal.md";
    expectHandedOver(markHref(path), path);
  });

  it("hands a bare relative path over instead of letting it render as blocked", () => {
    const path = "schema-drift-notes.md";
    expectHandedOver(markHref(path), path);
  });

  it("hands a nested relative path over", () => {
    const path = "docs/engineering/architecture/overview.md";
    expectHandedOver(markHref(path), path);
  });

  it("hands a dot-slash path over rather than letting it lose its relativity", () => {
    expectHandedOver(markHref("./docs/design.md"), "./docs/design.md");
  });

  it("hands over a path citing a line number, which is shaped like a scheme", () => {
    // `notes.md:12` parses as scheme "notes.md:" if you only look for a colon.
    expectHandedOver(markHref("docs/notes.md:12"), "docs/notes.md:12");
    expectHandedOver(markHref("notes.md:12:7"), "notes.md:12:7");
  });

  it.each([
    ["https://example.com/docs.md", "external URL"],
    ["http://localhost:3000/x.md", "plain http URL"],
    ["//cdn.example.com/x.md", "protocol-relative URL"],
    ["mailto:someone@example.com", "mailto"],
    ["#section-two", "in-page anchor"],
    ["javascript:alert(1)", "script scheme"],
    ["docs/page.md?raw=1", "path carrying a query"],
    ["docs/page.md#heading", "path carrying a fragment"],
  ])("leaves %s untouched (%s)", (href) => {
    expect(markHref(href)).toEqual({ href });
  });

  it("marks every file link in the tree, not just the first", () => {
    const first = anchor("one.md");
    const second = anchor("two.md");
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
    markWorkspaceFileLinks()(tree);
    expect(first.properties?.[WORKSPACE_FILE_LINK_ATTR]).toBe("one.md");
    expect(second.properties?.[WORKSPACE_FILE_LINK_ATTR]).toBe("two.md");
  });

  it("ignores non-anchor elements and hrefless anchors", () => {
    const img: TestNode = { type: "element", tagName: "img", properties: { src: "x.png" } };
    const bare: TestNode = { type: "element", tagName: "a", properties: {} };
    markWorkspaceFileLinks()({ type: "root", children: [img, bare] });
    expect(img.properties).toEqual({ src: "x.png" });
    expect(bare.properties).toEqual({});
  });
});
