// Reading where the reader is in a rich Markdown diff, from a few hit tests at the top of the
// review's view, and finding that place again after the preview re-wraps.
import { MARKDOWN_DIFF_SECTION_ATTRIBUTE } from "./markdownDiffSection";
import type { PreviewReadingAnchor } from "./previewWidthAnchor";
import { textOffsetWithin, textRange } from "./renderedRows";

/** The caret position the browser places at a point, where it can say; never moves the selection. */
function caretAt(x: number, y: number): { node: Node; offset: number } | null {
  const positioned = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
  };
  if (typeof positioned.caretPositionFromPoint === "function") {
    const position = positioned.caretPositionFromPoint(x, y);
    return position ? { node: position.offsetNode, offset: position.offset } : null;
  }
  if (typeof document.caretRangeFromPoint === "function") {
    const range = document.caretRangeFromPoint(x, y);
    return range ? { node: range.startContainer, offset: range.startOffset } : null;
  }
  return null;
}

/**
 * The viewport top of `section`'s character at text `offset`, counted as `textRange` counts, or
 * null when there's no such character with a size on screen.
 */
export function characterTop(section: Element, offset: number): number | null {
  // The character after the offset, or at the end of the text, the one before it.
  const range = textRange(section, offset, offset + 1) ?? textRange(section, offset - 1, offset);
  const rect = range?.getBoundingClientRect();
  return rect && rect.height > 0 ? rect.top : null;
}

/**
 * The character at a point already inside `section`: its text offset and viewport top. Null
 * when the browser can't place a caret there, or places it outside the section's text, or
 * somewhere with no size.
 */
export function readingCharacter(
  section: Element,
  x: number,
  y: number,
): { offset: number; top: number } | null {
  const caret = caretAt(x, y);
  if (!caret || !section.contains(caret.node)) return null;
  const offset = textOffsetWithin(section, caret.node, caret.offset);
  if (offset === undefined) return null;
  const top = characterTop(section, offset);
  return top === null ? null : { offset, top };
}

/** The height of a file's header, which sticks to the top of the review's view. */
export const DIFF_FILE_HEADER_HEIGHT = 44;

/** Hit tests at the view's top, at most: the next is just below what covered the last. */
const READING_PROBES = 3;
/** How far below a miss the next hit test looks. */
const PROBE_STEP = 48;

/**
 * How far down from the view's top `preview`'s sticky header covers, whether or not it's drawn
 * yet: none until its file reaches the view's top, and none past the preview's own bottom.
 */
function stickyHeaderCover(preview: Element, viewTop: number, headerHeight: number): number {
  if (headerHeight <= 0) return 0;
  const bounds = preview.getBoundingClientRect();
  // The header sits just above the preview until the file's top scrolls past the view's top.
  if (bounds.top - headerHeight >= viewTop) return 0;
  return Math.max(0, Math.min(headerHeight, bounds.bottom - viewTop));
}

/**
 * The rich diff section at the top of the review's view, if that's where the reader is, and
 * the first character of their line in it: a few hit tests, never a walk over every section.
 * With `stickyHeaderHeight`, a file's header covers the view's top once its file scrolls there.
 * The header is drawn a few frames after a scroll, so the band it covers is skipped even before
 * it's there; where no header will be, the reader's first visible line counts.
 */
export function readPreviewAnchor(
  container: HTMLElement,
  stickyHeaderHeight = 0,
): PreviewReadingAnchor | null {
  // Some environments, such as a DOM without layout, can't hit-test at all.
  if (typeof document.elementFromPoint !== "function") return null;
  const view = container.getBoundingClientRect();
  const x = view.left + view.width / 2;
  let below = 1;
  for (let probe = 0; probe < READING_PROBES; probe += 1) {
    const hit = document.elementFromPoint(x, view.top + below);
    const section = hit?.closest(`[${MARKDOWN_DIFF_SECTION_ATTRIBUTE}]`);
    if (!section) {
      below += PROBE_STEP;
      continue;
    }
    const preview = section.closest<HTMLElement>("[data-markdown-diff-path]");
    const version = section
      .closest("[data-markdown-diff-version]")
      ?.getAttribute("data-markdown-diff-version");
    const index = Number(section.getAttribute(MARKDOWN_DIFF_SECTION_ATTRIBUTE));
    if (!preview || !version || !Number.isInteger(index)) return null;
    const covered = stickyHeaderCover(preview, view.top, stickyHeaderHeight);
    if (below < covered) {
      below = covered + 4;
      continue;
    }
    // The start of the reader's line: a re-wrap keeps it in place, where the end of a short
    // line, which a point past its text snaps to, can move to the next line.
    const block = section.firstElementChild ?? section;
    const text = readingCharacter(
      section,
      block.getBoundingClientRect().left + 1,
      view.top + below,
    );
    return {
      path: preview.getAttribute("data-markdown-diff-path") ?? "",
      version,
      section: index,
      offset: section.getBoundingClientRect().top - view.top,
      width: preview.getBoundingClientRect().width,
      ...(text ? { text: { offset: text.offset, top: text.top - view.top } } : {}),
    };
  }
  return null;
}
