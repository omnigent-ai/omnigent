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

/** The part of `link` that `range` covers. */
function selectedPart(range: Range, link: Element): Range {
  const part = document.createRange();
  part.selectNodeContents(link);
  if (range.compareBoundaryPoints(Range.START_TO_START, part) > 0) {
    part.setStart(range.startContainer, range.startOffset);
  }
  if (range.compareBoundaryPoints(Range.END_TO_END, part) < 0) {
    part.setEnd(range.endContainer, range.endOffset);
  }
  return part;
}

/** Whether `content` has top-level children that only make sense inside `tag`. */
function needsContainer(content: Node, tag: string): boolean {
  return Array.from(content.childNodes).some((child) => {
    if (!(child instanceof Element)) return false;
    return CONTAINERS_OF[keptTag(child) ?? ""]?.includes(tag) ?? false;
  });
}

/** A shallow copy of `ancestor` to wrap copied content, numbering an `ol` from the first selected item. */
function wrapperFor(ancestor: Element, range: Range): Element {
  const wrapper = ancestor.cloneNode(false) as Element;
  if (ancestor.localName !== "ol") return wrapper;
  const items = Array.from(ancestor.children).filter((child) => child.localName === "li");
  const firstSelected = items.findIndex((item) => range.intersectsNode(item));
  const start = Number.parseInt(ancestor.getAttribute("start") ?? "1", 10);
  if (firstSelected > 0 && Number.isFinite(start)) {
    wrapper.setAttribute("start", String(start + firstSelected));
  }
  return wrapper;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** A pattern for `label` that tolerates the browser's whitespace rendering. */
function labelPattern(label: string): string {
  return escapeRegExp(label.trim()).replace(/\s+/g, "\\s+");
}

function countMatches(text: string, pattern: string): number {
  return text.match(new RegExp(pattern, "g"))?.length ?? 0;
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
  label: string;
  href: string;
  /** Times the label's text occurs between the previous link and this one. */
  earlierMatches: number;
}

/**
 * Rewrites each selected link's visible text in `plainText` as a markdown link.
 * Links are matched in document order, skipping plain occurrences of the same words.
 */
function linkifyPlainText(plainText: string, links: SelectedLink[]): string {
  let output = "";
  let cursor = 0;
  for (const { label, href, earlierMatches } of links) {
    const matcher = new RegExp(labelPattern(label), "g");
    matcher.lastIndex = cursor;
    let match = matcher.exec(plainText);
    for (let skipped = 0; match && skipped < earlierMatches; skipped++) {
      match = matcher.exec(plainText);
    }
    if (!match) continue;
    output += plainText.slice(cursor, match.index) + markdownLink(match[0], href);
    cursor = match.index + match[0].length;
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
  const links: SelectedLink[] = [];
  const gap = document.createRange();
  gap.setStart(range.startContainer, range.startOffset);
  for (const link of Array.from(root.querySelectorAll("a[href]"))) {
    const href = pasteableHref(link);
    if (!href || !range.intersectsNode(link)) continue;
    const part = selectedPart(range, link);
    const label = part.toString();
    // A selection edge can touch a link without selecting any of its text.
    if (!label.trim()) continue;
    gap.setEnd(part.startContainer, part.startOffset);
    links.push({ label, href, earlierMatches: countMatches(gap.toString(), labelPattern(label)) });
    gap.setStart(part.endContainer, part.endOffset);
  }
  if (!links.length) return null;

  // cloneContents keeps elements the range only partly covers, but not the
  // common ancestor itself or the elements above it, so re-add the ones that matter.
  let content: Node = range.cloneContents();
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
  return { html: container.innerHTML.trim(), text: linkifyPlainText(plainText, links) };
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
