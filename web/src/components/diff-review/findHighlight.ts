// Locates a find match in the diff's rendered DOM and paints it. The diff
// renders each file in a shadow root, so lines are found by their data
// attributes and painted with the CSS Custom Highlight API, which leaves the
// renderer's nodes untouched.

import { type SearchHighlightNames, searchHighlightsSupported } from "@/lib/textRangeSearch";
import type { ReviewSearchMatch } from "./findMatches";
import { MARKDOWN_DIFF_SECTION_ATTRIBUTE } from "./markdownDiffSection";
import { textRange } from "./renderedRows";

/** The session review's find highlights. */
export const DIFF_FIND_HIGHLIGHTS = {
  all: "diff-review-find",
  current: "diff-review-find-current",
} as const satisfies SearchHighlightNames;

/** A pull request review's find highlights, as upstream's Changes tab names them. */
export const PULL_REQUEST_FIND_HIGHLIGHTS = {
  all: "pr-find",
  current: "pr-find-active",
} as const satisfies SearchHighlightNames;

/** Styles the shadow roots need, since document `::highlight()` rules don't reach them. */
export const DIFF_FIND_HIGHLIGHT_CSS = `
  ::highlight(${DIFF_FIND_HIGHLIGHTS.current}),
  ::highlight(${PULL_REQUEST_FIND_HIGHLIGHTS.current}) {
    background-color: color-mix(in oklch, var(--warning) 70%, transparent);
  }
`;

/** One review's painted find ranges, under the names it paints with. */
interface OwnedHighlight {
  names: SearchHighlightNames;
  all: readonly Range[];
  current: Range | null;
}

// Highlights are page-wide, so each review owns its ranges here and every name is rebuilt
// from all of its owners: one review's paint or clear never touches another's.
const owners = new Map<object, OwnedHighlight>();

function rebuild(name: string) {
  const ranges: Range[] = [];
  for (const owned of owners.values()) {
    if (owned.names.all === name) ranges.push(...owned.all);
    if (owned.names.current === name && owned.current) ranges.push(owned.current);
  }
  if (ranges.length === 0) CSS.highlights.delete(name);
  else CSS.highlights.set(name, new Highlight(...ranges));
}

/** Replace `owner`'s find ranges with `ranges`, `current` among them as the current match. */
export function paintOwnedFindHighlights(
  owner: object,
  names: SearchHighlightNames,
  ranges: readonly Range[],
  current: Range | null,
): void {
  if (!searchHighlightsSupported()) return;
  const previous = owners.get(owner);
  owners.set(owner, { names, all: ranges.filter((range) => range !== current), current });
  const touched = new Set([names.all, names.current]);
  if (previous) touched.add(previous.names.all).add(previous.names.current);
  for (const name of touched) rebuild(name);
}

/** Remove `owner`'s find ranges, leaving every other owner's painted. */
export function clearOwnedFindHighlights(owner: object): void {
  if (!searchHighlightsSupported()) return;
  const previous = owners.get(owner);
  if (!previous) return;
  owners.delete(owner);
  rebuild(previous.names.all);
  rebuild(previous.names.current);
}

function findSearchTarget(root: ParentNode, match: ReviewSearchMatch): HTMLElement | undefined {
  if (match.target === "markdownPreview") {
    return (
      root.querySelector<HTMLElement>(
        `[${MARKDOWN_DIFF_SECTION_ATTRIBUTE}="${match.lineNumber}"]`,
      ) ?? undefined
    );
  }
  if (match.target === "currentPath") {
    return root.querySelector<HTMLElement>("[data-title] bdi") ?? undefined;
  }
  if (match.target === "previousPath") {
    return root.querySelector<HTMLElement>("[data-prev-name] bdi") ?? undefined;
  }
  const candidates = [...root.querySelectorAll<HTMLElement>(`[data-line="${match.lineNumber}"]`)];
  if (!match.side) return candidates[0];
  const sideAttribute = match.side === "additions" ? "[data-additions]" : "[data-deletions]";
  const lineType = match.side === "additions" ? "change-addition" : "change-deletion";
  return (
    candidates.find((candidate) => candidate.closest(sideAttribute) !== null) ??
    candidates.find((candidate) => candidate.dataset.lineType === lineType) ??
    candidates.find((candidate) => candidate.dataset.lineType?.startsWith("context")) ??
    candidates[0]
  );
}

/** The DOM range of `match` inside its rendered item, if that line is rendered. */
export function searchMatchRange(
  renderedItem: HTMLElement,
  match: ReviewSearchMatch,
): Range | undefined {
  // A rich diff renders in the item's own children, outside its shadow root.
  const root =
    match.target === "markdownPreview" ? renderedItem : (renderedItem.shadowRoot ?? renderedItem);
  const target = findSearchTarget(root, match);
  return target ? textRange(target, match.from, match.to) : undefined;
}

/** The first match at or below the viewport top among rendered items. */
export function firstRenderedMatchAtOrAfter(
  renderedItems: ReadonlyMap<string, HTMLElement>,
  matches: readonly ReviewSearchMatch[],
  viewportTop: number,
): number | undefined {
  for (let index = 0; index < matches.length; index += 1) {
    const match = matches[index]!;
    const element = renderedItems.get(match.id);
    const range = element ? searchMatchRange(element, match) : undefined;
    const bounds = range?.getBoundingClientRect?.();
    if (bounds && bounds.height > 0 && bounds.bottom >= viewportTop) return index;
  }
  return undefined;
}

/**
 * Paint `match` as `owner`'s current find match under `names`. Returns false when it isn't
 * rendered, leaving `owner`'s highlights as they were.
 */
export function paintCurrentMatch(
  owner: object,
  names: SearchHighlightNames,
  renderedItem: HTMLElement,
  match: ReviewSearchMatch,
): boolean {
  const range = searchMatchRange(renderedItem, match);
  if (!range) return false;
  paintOwnedFindHighlights(owner, names, [range], range);
  return true;
}
