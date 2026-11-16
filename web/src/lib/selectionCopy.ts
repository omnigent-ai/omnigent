import type { ClipboardEvent } from "react";

import type { RichTextPayload } from "./clipboard";
import { PASTEABLE_HREF } from "./copyMarkdown";

// Formatting that means the same thing once pasted. Everything else unwraps to
// its children, so layout wrappers and styling classes stay behind.
const KEPT_TAGS = new Set([
  "p",
  "br",
  "hr",
  "strong",
  "b",
  "em",
  "i",
  "u",
  "s",
  "del",
  "code",
  "pre",
  "kbd",
  "sup",
  "sub",
  "blockquote",
  "ul",
  "ol",
  "li",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "table",
  "thead",
  "tbody",
  "tfoot",
  "tr",
  "th",
  "td",
]);

// Controls, icons, media, and form fields carry nothing worth pasting.
const DROPPED_TAGS = new Set([
  "button",
  "svg",
  "img",
  "input",
  "select",
  "textarea",
  "script",
  "style",
  "template",
  "iframe",
  "object",
  "embed",
  "video",
  "audio",
  "canvas",
]);

// Inline ancestors that a selection inside them should still carry, e.g. a
// selection of just a link's text is still a link.
const WRAPPING_TAGS = new Set(["a", "strong", "b", "em", "i", "u", "s", "del", "code"]);

// Containers that copied children are invalid or meaningless without, e.g. list
// items selected across a list need their list.
const CONTAINERS_OF: Record<string, string[]> = {
  li: ["ul", "ol"],
  tr: ["table", "thead", "tbody", "tfoot"],
  td: ["tr"],
  th: ["tr"],
  thead: ["table"],
  tbody: ["table"],
  tfoot: ["table"],
  p: ["blockquote"],
  ul: ["blockquote"],
  ol: ["blockquote"],
  pre: ["blockquote"],
};

/** The pasteable href of `element` when it is a link that works outside the app. */
function pasteableHref(element: Element): string | null {
  if (element.localName !== "a") return null;
  const href = element.getAttribute("href");
  return href && PASTEABLE_HREF.test(href) ? href : null;
}

/** Streamdown renders bold as a styled span; paste targets need the real tag. */
function keptTag(element: Element): string | null {
  if (element.getAttribute("data-streamdown") === "strong") return "strong";
  return KEPT_TAGS.has(element.localName) ? element.localName : null;
}

function isChrome(element: Element): boolean {
  if (DROPPED_TAGS.has(element.localName)) return true;
  if (element.getAttribute("aria-hidden") === "true") return true;
  const role = element.getAttribute("data-streamdown") ?? "";
  return role === "code-block-header" || role.endsWith("-actions");
}

/** Copies `source`'s children into `target`, keeping only pasteable markup. */
function appendCleaned(source: Node, target: Node): void {
  for (const child of Array.from(source.childNodes)) {
    if (child.nodeType === Node.TEXT_NODE) {
      target.appendChild(document.createTextNode(child.textContent ?? ""));
      continue;
    }
    if (!(child instanceof Element) || isChrome(child)) continue;
    const href = pasteableHref(child);
    const tag = href ? "a" : keptTag(child);
    if (!tag) {
      appendCleaned(child, target);
      continue;
    }
    const element = document.createElement(tag);
    if (href) element.setAttribute("href", href);
    const start = tag === "ol" ? child.getAttribute("start") : null;
    if (start && /^\d+$/.test(start)) element.setAttribute("start", start);
    appendCleaned(child, element);
    target.appendChild(element);
  }
}

/** Whether `content` has top-level children that only make sense inside `tag`. */
function needsContainer(content: Node, tag: string): boolean {
  return Array.from(content.childNodes).some((child) => {
    if (!(child instanceof Element)) return false;
    return CONTAINERS_OF[keptTag(child) ?? ""]?.includes(tag) ?? false;
  });
}

/** Numbers the copy of ordered list `list` from its first item that `range` selects. */
function numberFromSelection(copy: Element, list: Element, range: Range): void {
  const items = Array.from(list.children).filter((child) => child.localName === "li");
  const firstSelected = items.findIndex((item) => range.intersectsNode(item));
  const start = Number.parseInt(list.getAttribute("start") ?? "1", 10);
  if (firstSelected > 0 && Number.isFinite(start)) {
    copy.setAttribute("start", String(start + firstSelected));
  }
}

/**
 * Renumbers ordered lists that the selection starts inside. `cloneContents`
 * copies the elements around the start point as the first child at each level.
 */
function numberPartialLists(content: DocumentFragment, range: Range): void {
  const startPath: Element[] = [];
  for (
    let node: Node | null = range.startContainer;
    node && node !== range.commonAncestorContainer;
    node = node.parentNode
  ) {
    if (node instanceof Element) startPath.unshift(node);
  }
  let copy = content.firstChild;
  for (const original of startPath) {
    if (!(copy instanceof Element)) return;
    if (original.localName === "ol") numberFromSelection(copy, original, range);
    copy = copy.firstChild;
  }
}

/** A shallow copy of `ancestor` to wrap copied content. */
function wrapperFor(ancestor: Element, range: Range): Element {
  const wrapper = ancestor.cloneNode(false) as Element;
  if (ancestor.localName === "ol") numberFromSelection(wrapper, ancestor, range);
  return wrapper;
}

const UNCOPIED_TEXT_TAGS = new Set(["script", "style", "template"]);

/** Whether the browser leaves text inside `element` out of a plain-text copy. */
function hidesText(element: Element | null, root: Element, cache: Map<Element, boolean>): boolean {
  if (!element) return false;
  let hidden = cache.get(element);
  if (hidden === undefined) {
    const style = window.getComputedStyle(element);
    hidden =
      UNCOPIED_TEXT_TAGS.has(element.localName) ||
      style.display === "none" ||
      style.userSelect === "none" ||
      (element !== root && hidesText(element.parentElement, root, cache));
    cache.set(element, hidden);
  }
  return hidden;
}

interface SelectedText {
  text: string;
  /** Where each copied text node's selected part sits in `text`. */
  spans: Map<Text, { start: number; end: number }>;
}

/**
 * The selected text that the browser's plain-text copy includes, minus the
 * line breaks it adds between blocks; alignment ignores whitespace.
 */
function selectedText(range: Range, root: Element): SelectedText {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const hiddenCache = new Map<Element, boolean>();
  const spans = new Map<Text, { start: number; end: number }>();
  let text = "";
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    if (!range.intersectsNode(node) || hidesText(node.parentElement, root, hiddenCache)) continue;
    const from = node === range.startContainer ? range.startOffset : 0;
    const to = node === range.endContainer ? range.endOffset : node.data.length;
    spans.set(node, { start: text.length, end: text.length + to - from });
    text += node.data.slice(from, to);
  }
  return { text, spans };
}

const isSpace = (character: string) => /\s/.test(character);

/**
 * Maps each non-space character of `rendered` to its index in `plain`, or
 * returns null when the two differ in anything but whitespace and case.
 */
function alignText(rendered: string, plain: string): number[] | null {
  const positions: number[] = new Array<number>(rendered.length).fill(-1);
  let cursor = 0;
  for (let index = 0; index < rendered.length; index++) {
    if (isSpace(rendered[index]!)) continue;
    while (cursor < plain.length && isSpace(plain[cursor]!)) cursor++;
    if (plain[cursor]?.toLowerCase() !== rendered[index]!.toLowerCase()) return null;
    positions[index] = cursor++;
  }
  return plain.slice(cursor).trim() ? null : positions;
}

/** Whether `label` is the URL itself, allowing for the renderer's href normalization. */
function spellsOutHref(label: string, href: string): boolean {
  if (label === href || `mailto:${label}` === href) return true;
  try {
    return new URL(label).href === href;
  } catch {
    return false;
  }
}

/** A markdown link, or the bare text when it already spells out the URL. */
function markdownLink(label: string, href: string): string {
  if (spellsOutHref(label, href)) return label;
  const escapedLabel = label.replace(/[\\[\]]/g, "\\$&");
  const escapedHref = href.replace(/[<>\\]/g, "\\$&");
  return `[${escapedLabel}](<${escapedHref}>)`;
}

interface SelectedLink {
  href: string;
  /** The link's selected text, as a span of the rendered selection text. */
  start: number;
  end: number;
}

/**
 * Rewrites each selected link's text in `plainText` as a markdown link, using
 * the link's position in `rendered`. Leaves `plainText` as copied when the two
 * texts cannot be aligned, rather than guess which words a link covers.
 */
function linkifyPlainText(plainText: string, rendered: string, links: SelectedLink[]): string {
  const positions = alignText(rendered, plainText);
  if (!positions) return plainText;
  let output = "";
  let cursor = 0;
  for (const { href, start, end } of links) {
    const mapped = positions.slice(start, end).filter((position) => position >= 0);
    if (!mapped.length || mapped[0]! < cursor) continue;
    const labelStart = mapped[0]!;
    const labelEnd = mapped[mapped.length - 1]! + 1;
    output += plainText.slice(cursor, labelStart);
    output += markdownLink(plainText.slice(labelStart, labelEnd), href);
    cursor = labelEnd;
  }
  return output + plainText.slice(cursor);
}

/**
 * The clipboard flavors for a selection inside `root` that includes a link a
 * paste target can follow: clean HTML that keeps each link, and the browser's
 * plain text (`plainText`) with those links written as markdown. Returns null
 * when no such link is selected, leaving the browser's own copy in place.
 */
export function selectionToLinkedPayload(
  range: Range,
  root: Element,
  plainText: string,
): RichTextPayload | null {
  const rendered = selectedText(range, root);
  const links: SelectedLink[] = [];
  for (const link of Array.from(root.querySelectorAll("a[href]"))) {
    const href = pasteableHref(link);
    if (!href || !range.intersectsNode(link)) continue;
    let start = Infinity;
    let end = -Infinity;
    const walker = document.createTreeWalker(link, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const span = rendered.spans.get(node as Text);
      if (!span) continue;
      start = Math.min(start, span.start);
      end = Math.max(end, span.end);
    }
    // A selection edge can touch a link without selecting any of its text.
    if (end < start || !rendered.text.slice(start, end).trim()) continue;
    links.push({ href, start, end });
  }
  if (!links.length) return null;

  // cloneContents keeps elements the range only partly covers, but not the
  // common ancestor itself or the elements above it, so re-add the ones that matter.
  const copied = range.cloneContents();
  numberPartialLists(copied, range);
  let content: Node = copied;
  const common = range.commonAncestorContainer;
  for (
    let ancestor = common instanceof Element ? common : common.parentElement;
    ancestor && ancestor !== root && root.contains(ancestor);
    ancestor = ancestor.parentElement
  ) {
    const tag = pasteableHref(ancestor) ? "a" : keptTag(ancestor);
    if (!tag || !(WRAPPING_TAGS.has(tag) || needsContainer(content, tag))) continue;
    const wrapper = wrapperFor(ancestor, range);
    wrapper.appendChild(content);
    const holder = document.createDocumentFragment();
    holder.appendChild(wrapper);
    content = holder;
  }

  const container = document.createElement("div");
  appendCleaned(content, container);
  return {
    html: container.innerHTML.trim(),
    text: linkifyPlainText(plainText, rendered.text, links),
  };
}

/**
 * `onCopy` handler for rendered agent markdown. A selection that includes an
 * external or mailto link is copied as HTML with working links plus markdown
 * links in plain text. Other selections keep the browser's default copy.
 */
export function copySelectionWithLinks(event: ClipboardEvent<HTMLElement>): void {
  const selection = window.getSelection();
  // Multiple ranges (Firefox's ctrl-select) have no single reading order.
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return;
  const range = selection.getRangeAt(0);
  const root = event.currentTarget;
  if (!root.contains(range.commonAncestorContainer)) return;
  let payload: RichTextPayload | null;
  try {
    payload = selectionToLinkedPayload(range, root, selection.toString());
  } catch {
    // A conversion failure must not cost the user their copy.
    return;
  }
  if (!payload) return;
  event.preventDefault();
  event.clipboardData.setData("text/plain", payload.text);
  event.clipboardData.setData("text/html", payload.html);
}
