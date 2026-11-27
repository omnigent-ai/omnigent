import type { Element, Root } from "hast";
import { describe, expect, it } from "vitest";

import {
  MENTION_TAG,
  PULL_REQUEST_REFERENCE_TAG,
  rehypeMentions,
  rehypePullRequestReferences,
} from "./rehype";

const text = (value: string) => ({ type: "text" as const, value });
const element = (tagName: string, children: Element["children"], properties = {}): Element => ({
  type: "element",
  tagName,
  properties,
  children,
});

function marked(
  tree: Root,
  plugin = rehypePullRequestReferences,
  tagName = PULL_REQUEST_REFERENCE_TAG,
): string[] {
  plugin()(tree);
  const found: string[] = [];
  const visit = (node: Root | Element) => {
    for (const child of node.children) {
      if (child.type !== "element") continue;
      if (child.tagName === tagName) {
        found.push(child.children.map((part) => (part.type === "text" ? part.value : "")).join(""));
      }
      visit(child);
    }
  };
  visit(tree);
  return found;
}

describe("rehypePullRequestReferences", () => {
  it("marks references in prose and keeps the text around them", () => {
    const paragraph = element("p", [text("Stacked on #12 and acme/app#7, then done.")]);
    const tree: Root = { type: "root", children: [paragraph] };

    expect(marked(tree)).toEqual(["#12", "acme/app#7"]);
    expect(paragraph.children.map((part) => (part.type === "text" ? part.value : "*"))).toEqual([
      "Stacked on ",
      "*",
      " and ",
      "*",
      ", then done.",
    ]);
  });

  it("leaves code, code blocks and links as written", () => {
    const tree: Root = {
      type: "root",
      children: [
        element("p", [
          element("code", [text("#1")]),
          text(" "),
          element("a", [text("#2")], { href: "https://example.com" }),
        ]),
        element("pre", [element("code", [text("fixes #3")])]),
        element("ul", [element("li", [element("strong", [text("see #4")])])]),
      ],
    };

    expect(marked(tree)).toEqual(["#4"]);
  });
});

describe("rehypeMentions", () => {
  const mentions = (tree: Root) => marked(tree, rehypeMentions, MENTION_TAG);

  it("marks mentions in prose, beside the PR references", () => {
    const paragraph = element("p", [text("@octo, see #12 with @acme/web and @octo-user_acme.")]);
    const tree: Root = { type: "root", children: [paragraph] };
    rehypePullRequestReferences()(tree);

    expect(mentions(tree)).toEqual(["@octo", "@acme/web", "@octo-user_acme"]);
    expect(marked(tree)).toEqual(["#12"]);
  });

  it("leaves code, links and email addresses as written", () => {
    const tree: Root = {
      type: "root",
      children: [
        element("p", [
          element("code", [text("@octo")]),
          element("a", [text("@octo")], { href: "https://example.com" }),
          text(" octo@example.com octo_acme@example.com @octo_ @octo__acme café@octo @octoé"),
        ]),
        element("pre", [element("code", [text("@decorator")])]),
      ],
    };

    expect(mentions(tree)).toEqual([]);
  });
});
