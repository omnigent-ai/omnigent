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

// Icons, media, and form fields carry nothing worth pasting. Buttons unwrap to
// their text (file mentions are buttons), and images paste as their alt text.
const DROPPED_TAGS = new Set([
  "svg",
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

/** Whether `content` has top-level children that only make sense inside `tag`. */
function needsContainer(content: Node, tag: string): boolean {
  return Array.from(content.childNodes).some((child) => {
    if (!(child instanceof Element)) return false;
    return CONTAINERS_OF[keptTag(child) ?? ""]?.includes(tag) ?? false;
  });
}

// MathML annotations hold a formula's source, which browsers don't render.
// Kept tags that a range boundary can touch without selecting their content.
const BLOCK_TAGS = new Set(["p", "pre", "blockquote", "ul", "ol", "li", "table", "tr", "td", "th"]);
for (const level of [1, 2, 3, 4, 5, 6]) BLOCK_TAGS.add(`h${level}`);
for (const section of ["thead", "tbody", "tfoot"]) BLOCK_TAGS.add(section);

const UNCOPIED_TEXT_TAGS = new Set(["script", "style", "template", "annotation"]);

/** Whether the browser leaves text inside `element` out of a copy. */
function hidesText(element: Element | null, root: Element, cache: Map<Element, boolean>): boolean {
  if (!element) return false;
  let hidden = cache.get(element);
  if (hidden === undefined) {
    const styled = element instanceof HTMLElement || element instanceof SVGElement;
    const style = styled ? window.getComputedStyle(element) : null;
    hidden =
      UNCOPIED_TEXT_TAGS.has(element.localName) ||
      style?.display === "none" ||
      style?.userSelect === "none" ||
      (element !== root && hidesText(element.parentElement, root, cache));
    cache.set(element, hidden);
  }
  return hidden;
}

interface TextSpan {
  start: number;
  end: number;
}

/** A selection inside a reply, with the text the browser's copy includes. */
interface SelectionView {
  range: Range;
  isHidden: (element: Element | null) => boolean;
  /** The copied text, minus the line breaks the browser adds between blocks. */
  text: string;
  /** Where each copied text node's selected part sits in `text`. */
  spans: Map<Text, TextSpan>;
}

/** The part of text node `node` that `range` selects, as offsets into its data. */
function selectedOffsets(node: Text, range: Range): TextSpan {
  return {
    start: node === range.startContainer ? range.startOffset : 0,
    end: node === range.endContainer ? range.endOffset : node.data.length,
  };
}

function viewSelection(range: Range, root: Element, common: Element): SelectionView {
  const hiddenCache = new Map<Element, boolean>();
  const isHidden = (element: Element | null) => hidesText(element, root, hiddenCache);
  const spans = new Map<Text, TextSpan>();
  let text = "";
  const walker = document.createTreeWalker(common, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    if (!range.intersectsNode(node) || isHidden(node.parentElement)) continue;
    const { start, end } = selectedOffsets(node, range);
    spans.set(node, { start: text.length, end: text.length + end - start });
    text += node.data.slice(start, end);
  }
  return { range, isHidden, text, spans };
}

/** Where `element`'s copied text sits in the view's text, or null if none is copied. */
function selectedSpan(element: Element, view: SelectionView): TextSpan | null {
  let start = Infinity;
  let end = -Infinity;
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const span = view.spans.get(node as Text);
    if (!span) continue;
    start = Math.min(start, span.start);
    end = Math.max(end, span.end);
  }
  return end > start && view.text.slice(start, end).trim() ? { start, end } : null;
}

/** Whether the selection covers `element` or copies some of its text, not just touches it. */
function isSelected(element: Element, view: SelectionView): boolean {
  const whole = document.createRange();
  whole.selectNode(element);
  const covers =
    view.range.compareBoundaryPoints(Range.START_TO_START, whole) <= 0 &&
    view.range.compareBoundaryPoints(Range.END_TO_END, whole) >= 0;
  return covers || selectedSpan(element, view) !== null;
}

/** An empty `tag` element for `source`, with only the attributes worth pasting. */
function cleanElement(source: Element, tag: string, view: SelectionView): Element {
  const element = document.createElement(tag);
  const href = tag === "a" ? pasteableHref(source) : null;
  if (href) element.setAttribute("href", href);
  if (tag === "ol") {
    // Number the copy from its first selected item, as the reply showed it.
    const items = Array.from(source.children).filter((child) => child.localName === "li");
    const skipped = Math.max(
      0,
      items.findIndex((item) => isSelected(item, view)),
    );
    const start = Number.parseInt(source.getAttribute("start") ?? "1", 10) + skipped;
    if (Number.isFinite(start) && start !== 1) element.setAttribute("start", String(start));
  }
  return element;
}

/** Appends the selected part of `source`'s children to `target` as pasteable markup. */
function appendSelected(source: Node, target: Node, view: SelectionView): void {
  for (const child of Array.from(source.childNodes)) {
    if (!view.range.intersectsNode(child)) continue;
    if (child instanceof Text) {
      const { start, end } = selectedOffsets(child, view.range);
      if (end > start) target.appendChild(document.createTextNode(child.data.slice(start, end)));
      continue;
    }
    if (!(child instanceof Element) || isChrome(child) || view.isHidden(child)) continue;
    // KaTeX renders each formula twice (MathML and HTML); paste its TeX source once.
    const tex = child.classList.contains("katex")
      ? child.querySelector('annotation[encoding="application/x-tex"]')?.textContent
      : null;
    const replacement = tex ?? (child.localName === "img" ? child.getAttribute("alt") : null);
    if (replacement != null) {
      if (replacement) target.appendChild(document.createTextNode(replacement));
      continue;
    }
    const tag = pasteableHref(child) ? "a" : keptTag(child);
    // A range edge at a block's start or end touches it without selecting it.
    if (tag && BLOCK_TAGS.has(tag) && !isSelected(child, view)) continue;
    if (!tag) {
      appendSelected(child, target, view);
      continue;
    }
    const element = cleanElement(child, tag, view);
    appendSelected(child, element, view);
    target.appendChild(element);
  }
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

interface SelectedLink extends TextSpan {
  href: string;
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
  const commonNode = range.commonAncestorContainer;
  const common = commonNode instanceof Element ? commonNode : commonNode.parentElement;
  if (!common) return null;
  const view = viewSelection(range, root, common);
  const links: SelectedLink[] = [];
  for (const link of Array.from(root.querySelectorAll("a[href]"))) {
    const href = pasteableHref(link);
    if (!href || !range.intersectsNode(link)) continue;
    // A selection edge can touch a link without selecting any of its text.
    const span = selectedSpan(link, view);
    if (span) links.push({ href, ...span });
  }
  if (!links.length) return null;

  // The walk copies what the selection covers below its common ancestor; re-add
  // the ancestors that matter, e.g. the link around a selection of its text.
  let content: Node = document.createDocumentFragment();
  appendSelected(common, content, view);
  for (
    let ancestor: Element | null = common;
    ancestor && ancestor !== root && root.contains(ancestor);
    ancestor = ancestor.parentElement
  ) {
    const tag = pasteableHref(ancestor) ? "a" : keptTag(ancestor);
    if (!tag || !(WRAPPING_TAGS.has(tag) || needsContainer(content, tag))) continue;
    const wrapper = cleanElement(ancestor, tag, view);
    wrapper.appendChild(content);
    content = document.createDocumentFragment();
    content.appendChild(wrapper);
  }

  const container = document.createElement("div");
  container.appendChild(content);
  // Drop the line breaks between rendered blocks, but keep selected spaces.
  const html = container.innerHTML.replace(/^\s*\n\s*|\s*\n\s*$/g, "");
  return { html, text: linkifyPlainText(plainText, view.text, links) };
}

/** `range` limited to `root`, or null when it selects text outside `root`. */
function clipToRoot(range: Range, root: Element): Range | null {
  const inside = document.createRange();
  inside.selectNodeContents(root);
  const clipped = range.cloneRange();
  if (range.compareBoundaryPoints(Range.START_TO_START, inside) < 0) {
    const before = range.cloneRange();
    before.setEnd(inside.startContainer, inside.startOffset);
    if (before.toString().trim()) return null;
    clipped.setStart(inside.startContainer, inside.startOffset);
  }
  if (range.compareBoundaryPoints(Range.END_TO_END, inside) > 0) {
    const after = range.cloneRange();
    after.setStart(inside.endContainer, inside.endOffset);
    if (after.toString().trim()) return null;
    clipped.setEnd(inside.endContainer, inside.endOffset);
  }
  return clipped.collapsed ? null : clipped;
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
  let payload: RichTextPayload | null;
  try {
    // A triple-click can end the range at the start of the next block.
    const range = clipToRoot(selection.getRangeAt(0), event.currentTarget);
    if (!range) return;
    payload = selectionToLinkedPayload(range, event.currentTarget, selection.toString());
  } catch {
    // A conversion failure must not cost the user their copy.
    return;
  }
  if (!payload) return;
  try {
    event.clipboardData.setData("text/plain", payload.text);
    event.clipboardData.setData("text/html", payload.html);
  } catch {
    return;
  }
  // Cancel the browser's copy only once both flavors are written.
  event.preventDefault();
}
