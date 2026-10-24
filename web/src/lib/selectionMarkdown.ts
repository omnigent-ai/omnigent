import { fromDom } from "hast-util-from-dom";
import { toMdast } from "hast-util-to-mdast";
import { gfmToMarkdown } from "mdast-util-gfm";
import { toMarkdown } from "mdast-util-to-markdown";

// Controls Streamdown renders inside a block (copy/download buttons, the code
// block's language label) that aren't part of the agent's text.
const CHROME_SELECTOR = "button, svg, [data-streamdown='code-block-header']";

/**
 * Converts the rendered markdown a selection covers back into markdown source.
 *
 * The browser's own plain-text flavor drops list markers — bullets and numbers
 * are CSS `::marker` boxes, not text — so a pasted list arrives as bare lines.
 * Returns `null` when the selection holds no list item, leaving the browser's
 * copy alone.
 *
 * @param range - The selected range; must lie inside `root`.
 * @param root - The rendered markdown container the selection is scoped to.
 */
export function selectionToMarkdown(range: Range, root: Element): string | null {
  if (!root.contains(range.commonAncestorContainer)) return null;
  const fragment = wrapInSelectedList(range, root);
  if (!fragment.querySelector("li")) return null;
  normalizeRenderedMarkdown(fragment);
  const markdown = toMarkdown(toMdast(fromDom(fragment)), {
    bullet: "-",
    extensions: [gfmToMarkdown()],
  });
  return markdown.trim();
}

/**
 * Clones the selected content. A selection spanning items of one list clones
 * just the `<li>`s, so they're re-wrapped in their list — keeping an ordered
 * list's numbering from the first selected item on.
 */
function wrapInSelectedList(range: Range, root: Element): DocumentFragment {
  const contents = range.cloneContents();
  const ancestor = range.commonAncestorContainer;
  if (!(ancestor instanceof HTMLUListElement || ancestor instanceof HTMLOListElement)) {
    return contents;
  }
  if (ancestor === root) return contents;
  const list = ancestor.cloneNode(false) as HTMLUListElement | HTMLOListElement;
  if (list instanceof HTMLOListElement && ancestor instanceof HTMLOListElement) {
    const items = Array.from(ancestor.children).filter((child) => child.tagName === "LI");
    const firstSelected = Math.max(
      0,
      items.findIndex((item) => range.intersectsNode(item)),
    );
    list.start = ancestor.start + firstSelected;
  }
  list.append(contents);
  const fragment = document.createDocumentFragment();
  fragment.append(list);
  return fragment;
}

/** Undoes Streamdown's rendering choices that the HTML-to-markdown step can't read. */
function normalizeRenderedMarkdown(fragment: DocumentFragment): void {
  for (const element of fragment.querySelectorAll(CHROME_SELECTOR)) element.remove();
  // Bold renders as a styled span rather than <strong>.
  for (const span of fragment.querySelectorAll("[data-streamdown='strong']")) {
    const strong = document.createElement("strong");
    strong.append(...span.childNodes);
    span.replaceWith(strong);
  }
  for (const pre of fragment.querySelectorAll("pre")) {
    const code = pre.querySelector("code");
    if (!code) continue;
    // Highlighted code puts each line in its own block span with no newline
    // between them; the language rides on the <pre>, not the <code>.
    const lines = Array.from(code.children).filter((child) => child.tagName === "SPAN");
    lines.slice(0, -1).forEach((line) => line.after("\n"));
    const language = Array.from(pre.classList).find((name) => name.startsWith("language-"));
    if (language) code.classList.add(language);
  }
}
