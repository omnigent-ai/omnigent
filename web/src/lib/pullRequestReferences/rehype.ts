// Marks the PR references and mentions written in rendered Markdown's prose, such as `#123` and
// `@login`, so a renderer can show each with its status or link. Code, links and headings'
// anchors stay as written. It adds its own elements, so it runs after sanitizing.

import type { Element, ElementContent, Root as HastRoot, RootContent as HastContent } from "hast";

import { detectPullRequestShorthand } from "./detect";
import { detectMentions } from "./mentions";

/** The element each reference becomes; its only child is the reference as written. */
export const PULL_REQUEST_REFERENCE_TAG = "pull-request-reference";
/** The element each mention becomes; its only child is the mention as written. */
export const MENTION_TAG = "code-host-mention";

/** HTML whose text is never marked: code, what's already a link, and what's already marked. */
const VERBATIM_TAGS = new Set([
  "a",
  "code",
  "pre",
  "kbd",
  "samp",
  "script",
  "style",
  "textarea",
  PULL_REQUEST_REFERENCE_TAG,
  MENTION_TAG,
]);

function markedElement(tagName: string, text: string): Element {
  return {
    type: "element",
    tagName,
    properties: {},
    children: [{ type: "text", value: text }],
  };
}

function markInChildren(
  children: HastContent[],
  tagName: string,
  detect: (text: string) => readonly { start: number; end: number }[],
): void {
  for (let index = 0; index < children.length; index += 1) {
    const child = children[index]!;
    if (child.type === "element") {
      if (!VERBATIM_TAGS.has(child.tagName)) markInChildren(child.children, tagName, detect);
      continue;
    }
    if (child.type !== "text") continue;
    const found = detect(child.value);
    if (found.length === 0) continue;
    const parts: ElementContent[] = [];
    let at = 0;
    for (const { start, end } of found) {
      if (start > at) parts.push({ type: "text", value: child.value.slice(at, start) });
      parts.push(markedElement(tagName, child.value.slice(start, end)));
      at = end;
    }
    if (at < child.value.length) parts.push({ type: "text", value: child.value.slice(at) });
    children.splice(index, 1, ...parts);
    index += parts.length - 1;
  }
}

/** The rehype plugin: wraps each reference in prose in a {@link PULL_REQUEST_REFERENCE_TAG}. */
export function rehypePullRequestReferences() {
  return (tree: HastRoot) =>
    markInChildren(tree.children, PULL_REQUEST_REFERENCE_TAG, detectPullRequestShorthand);
}

/** The rehype plugin: wraps each mention in prose in a {@link MENTION_TAG}. */
export function rehypeMentions() {
  return (tree: HastRoot) => markInChildren(tree.children, MENTION_TAG, detectMentions);
}
