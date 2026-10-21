// Agents link files they just wrote. Before the marking pass, the harden step
// either kept such a link as a real anchor (clicking it navigated the app origin
// and 404'd), or stripped the href and appended " [blocked]", which read as
// though the app had censored it. These cases pin which hrefs get handed to the
// FileViewer renderer and which are still left to harden untouched.

import { describe, expect, it } from "vitest";
import {
  markWorkspaceFileLinks,
  rewriteFileUriLinks,
  WORKSPACE_FILE_LINK_ATTR,
} from "./streamdown-security";

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

/** Runs the file-URI rewrite and then the marking pass, in their configured order. */
function rewriteThenMarkHref(href: string): Record<string, unknown> {
  const node = anchor(href);
  const tree: TestNode = {
    type: "root",
    children: [{ type: "element", tagName: "p", children: [node] }],
  };
  rewriteFileUriLinks()(tree);
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
    "docs/notes.md#L12",
    "docs/notes.md#L12C7",
    "docs/notes.md#L12-L18",
    "docs/notes.md#L12C7-L18C2",
  ])("hands over a path with a source-style line fragment: %s", (path) => {
    expectHandedOver(markHref(path), path);
  });

  it("decodes a percent-encoded href so the stored path matches the file on disk", () => {
    // A link to a file with spaces/`+` in its name arrives percent-encoded;
    // left encoded, the FileViewer lookup can never match the real filename.
    expectHandedOver(
      markHref("customer-notes/SAP%20-%20MLflow%20Labeling%20+%20Review%20Queues.md"),
      "customer-notes/SAP - MLflow Labeling + Review Queues.md",
    );
  });

  it("decodes a percent-encoded path while preserving its line fragment", () => {
    expectHandedOver(markHref("docs/My%20Notes.md#L12"), "docs/My Notes.md#L12");
    expectHandedOver(markHref("docs/My%20Notes.md:12:7"), "docs/My Notes.md:12:7");
  });

  it.each([
    ["docs/report.md%23L12", "encoded # would read as a line fragment"],
    ["docs/report.md%3A12", "encoded : would read as a line number"],
  ])("keeps %s encoded rather than decoding it into a citation (%s)", (href) => {
    // The opener re-splits the stored path, so a decoded `report.md#L12` would
    // open `report.md` at line 12 — a different file. Left encoded, the
    // literal filename fails its lookup exactly as it did before.
    expectHandedOver(markHref(href), href);
  });

  it("keeps a malformed-encoding href rather than dropping the link", () => {
    // A lone `%` is a legal filename character but invalid percent-encoding;
    // decodeURIComponent throws on it, so the raw href is kept.
    expectHandedOver(markHref("docs/50%-done.md"), "docs/50%-done.md");
  });

  describe("after rewriteFileUriLinks", () => {
    it("decodes a file: URI exactly once, so a literal-percent filename survives", () => {
      // `rewriteFileUriLinks` runs first in the configured plugin order. A file
      // literally named `report%20final.md` is linked as `report%2520final.md`;
      // decoding in both passes would open `report final.md` instead.
      expectHandedOver(
        rewriteThenMarkHref("file:///ws/report%2520final.md"),
        "/ws/report%20final.md",
      );
    });

    it("decodes a file: URI with spaces once and keeps its line fragment", () => {
      expectHandedOver(rewriteThenMarkHref("file:///ws/My%20Notes.md#L12"), "/ws/My Notes.md#L12");
    });

    it("decodes a basename citation once after its colon suffix is rewritten", () => {
      expectHandedOver(rewriteThenMarkHref("My%20Notes.md:12"), "My Notes.md#L12");
    });
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
    ["docs/page.md#Lx", "malformed line fragment"],
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
