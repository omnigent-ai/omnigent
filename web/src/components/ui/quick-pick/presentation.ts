// Pure presentation for QuickPick: turns segments of items, the query and the
// recently picked items into the ordered rows the list renders, plus keyboard navigation.

import type { MatchRange } from "./match";
import {
  compareItemMatches,
  type FuzzyMatching,
  itemQueryTokens,
  type ItemMatch,
  matchItem,
  MatchTier,
} from "./itemRanking";
import type {
  QuickPickHeadingRow,
  QuickPickItem,
  QuickPickItemRow,
  QuickPickRanking,
  QuickPickRow,
} from "./model";

/**
 * With a query: `source` keeps each group together, in the order the items list
 * them; `match` ranks rows across groups, each naming its group, so a group
 * never holds a better match below weaker ones.
 */
export type QuickPickGroupOrder = "source" | "match";

/** One run of items that rank together, such as commands or one group of search results. */
export interface QuickPickSegment<Value> {
  sourceId: string;
  ranking: QuickPickRanking;
  /** Defaults to "fuzzy". */
  fuzzyMatching?: FuzzyMatching;
  /** Defaults to "source". */
  groupOrder?: QuickPickGroupOrder;
  /** `source` for a segment an async source loaded, which `sourcesFirst` moves ahead. */
  origin?: "items" | "source";
  items: readonly QuickPickItem<Value>[];
}

export interface QuickPickPresentationInput<Value> {
  segments: readonly QuickPickSegment<Value>[];
  query: string;
  /** Item IDs, most recent first. */
  recentIds?: readonly string[];
  /** The row made from the typed query, listed first. */
  freeTextItem?: QuickPickItem<Value> | null;
  /**
   * Async sources' rows lead the other segments, unless an item matches the whole query from
   * a word's start, such as a typed command name, which keeps the items first.
   */
  sourcesFirst?: boolean;
}

export const RECENT_HEADING_LABEL = "Recent";
// Recency only breaks ties between equal-quality matches; beyond this it stops mattering.
const RECENCY_TIEBREAK_LIMIT = 50;

const NO_RANGES: readonly MatchRange[] = [];

function itemRow<Value>(
  item: QuickPickItem<Value>,
  ranges?: Record<string, MatchRange[]>,
  { isFreeText = false, sectionLabel }: { isFreeText?: boolean; sectionLabel?: string } = {},
): QuickPickItemRow<Value> {
  // Client ranges replace provider highlights, so a field the query missed stays plain.
  const source = ranges ?? item.highlights;
  return {
    kind: "item",
    key: `item:${item.id}`,
    item,
    labelRanges: source?.label ?? NO_RANGES,
    descriptionRanges: source?.description ?? NO_RANGES,
    isFreeText,
    ...(sectionLabel !== undefined ? { sectionLabel } : {}),
  };
}

function headingRow(sourceId: string, label: string): QuickPickHeadingRow {
  return { kind: "heading", key: `heading:${JSON.stringify([sourceId, label])}`, label };
}

// Groups appear in order of first appearance; ungrouped items get no heading.
// An ID already in `seen` is skipped, so the first visible row for it wins and a
// group left without rows gets no heading.
function groupedRows<Value>(
  sourceId: string,
  sourceOrder: readonly QuickPickItem<Value>[],
  rowsByItem: Map<QuickPickItem<Value>, QuickPickItemRow<Value>>,
  orderedItems: readonly QuickPickItem<Value>[],
  seen: Set<string>,
): QuickPickRow<Value>[] {
  const groupOrder: (string | undefined)[] = [];
  const seenGroups = new Set<string | undefined>();
  for (const item of sourceOrder) {
    if (!seenGroups.has(item.group)) {
      seenGroups.add(item.group);
      groupOrder.push(item.group);
    }
  }
  const itemsByGroup = new Map<string | undefined, QuickPickItem<Value>[]>();
  for (const item of orderedItems) {
    const members = itemsByGroup.get(item.group);
    if (members) members.push(item);
    else itemsByGroup.set(item.group, [item]);
  }
  const rows: QuickPickRow<Value>[] = [];
  for (const group of groupOrder) {
    const heading = group === undefined ? undefined : headingRow(sourceId, group);
    const groupRows: QuickPickRow<Value>[] = [];
    for (const item of itemsByGroup.get(group) ?? []) {
      const row = rowsByItem.get(item);
      if (!row || seen.has(item.id)) continue;
      seen.add(item.id);
      groupRows.push(heading ? { ...row, headingKey: heading.key } : row);
    }
    if (groupRows.length === 0) continue;
    if (heading) rows.push(heading);
    rows.push(...groupRows);
  }
  return rows;
}

interface RankedItem<Value> {
  item: QuickPickItem<Value>;
  match: ItemMatch;
  recency: number;
  sourceIndex: number;
}

function compareRanked<Value>(first: RankedItem<Value>, second: RankedItem<Value>): number {
  return (
    compareItemMatches(first.match, second.match) ||
    Math.min(first.recency, RECENCY_TIEBREAK_LIMIT) -
      Math.min(second.recency, RECENCY_TIEBREAK_LIMIT) ||
    first.sourceIndex - second.sourceIndex
  );
}

function rankSegment<Value>(
  segment: QuickPickSegment<Value>,
  query: string,
  recency: ReadonlyMap<string, number>,
  seen: Set<string>,
): QuickPickRow<Value>[] {
  const tokens = itemQueryTokens(query);
  const ranked: RankedItem<Value>[] = [];
  segment.items.forEach((item, sourceIndex) => {
    const match = matchItem(tokens, item, segment.fuzzyMatching);
    if (match) {
      const itemRecency = recency.get(item.id) ?? Number.POSITIVE_INFINITY;
      ranked.push({ item, match, recency: itemRecency, sourceIndex });
    }
  });
  ranked.sort(compareRanked);
  if (segment.groupOrder === "match") {
    // Without headings, each row names its group in their place.
    return ranked.flatMap(({ item, match }) => {
      if (seen.has(item.id)) return [];
      seen.add(item.id);
      return [itemRow(item, match.candidate.ranges, { sectionLabel: item.group })];
    });
  }
  const rowsByItem = new Map(
    ranked.map(({ item, match }) => [item, itemRow(item, match.candidate.ranges)]),
  );
  return groupedRows(
    segment.sourceId,
    segment.items,
    rowsByItem,
    ranked.map(({ item }) => item),
    seen,
  );
}

function unrankedSegment<Value>(
  segment: QuickPickSegment<Value>,
  seen: Set<string>,
): QuickPickRow<Value>[] {
  const rowsByItem = new Map(segment.items.map((item) => [item, itemRow(item)]));
  return groupedRows(segment.sourceId, segment.items, rowsByItem, segment.items, seen);
}

/** Whether an item's label in `segment` matches the whole query from a word's start. */
function hasLeadingMatch<Value>(segment: QuickPickSegment<Value>, query: string): boolean {
  const tokens = itemQueryTokens(query);
  return segment.items.some((item) => {
    if (item.leadsSources === false) return false;
    const match = matchItem(tokens, item, segment.fuzzyMatching);
    if (!match || match.tier > MatchTier.labelWordPrefix) return false;
    return tokens.length === 1 || match.candidate.wholeQuery;
  });
}

function orderSegments<Value>(
  segments: readonly QuickPickSegment<Value>[],
  searching: boolean,
  query: string,
): readonly QuickPickSegment<Value>[] {
  const fromSources = segments.filter((segment) => segment.origin === "source");
  const others = segments.filter((segment) => segment.origin !== "source");
  const itemsLead = searching && others.some((segment) => hasLeadingMatch(segment, query));
  return itemsLead ? [...others, ...fromSources] : [...fromSources, ...others];
}

// Recently picked items of the client-ranked segments, most recent first, under one heading.
function recentRows<Value>(
  segments: readonly QuickPickSegment<Value>[],
  recency: ReadonlyMap<string, number>,
  seen: Set<string>,
): QuickPickRow<Value>[] {
  const recentItems = segments
    .filter((segment) => segment.ranking === "client")
    .flatMap((segment) =>
      segment.items.filter((item) => recency.has(item.id) && item.rememberInHistory !== false),
    )
    .sort((first, second) => (recency.get(first.id) ?? 0) - (recency.get(second.id) ?? 0));
  const heading: QuickPickHeadingRow = {
    kind: "heading",
    key: "heading:recent",
    label: RECENT_HEADING_LABEL,
  };
  const rows: QuickPickRow<Value>[] = [];
  for (const item of recentItems) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    rows.push({ ...itemRow(item), headingKey: heading.key });
  }
  return rows.length === 0 ? rows : [heading, ...rows];
}

/**
 * Without a query, recently picked items first and then every segment in source
 * order. With one, client-ranked segments keep only matching items, best first
 * within each group, with recency breaking ties.
 */
export function buildQuickPickRows<Value>({
  segments: givenSegments,
  query,
  recentIds = [],
  freeTextItem = null,
  sourcesFirst = false,
}: QuickPickPresentationInput<Value>): QuickPickRow<Value>[] {
  const searching = query.trim().length > 0;
  // The first row for an ID wins, such as a session two searches both return.
  const seen = new Set<string>();
  const rows: QuickPickRow<Value>[] = [];
  if (freeTextItem) {
    seen.add(freeTextItem.id);
    rows.push(itemRow(freeTextItem, undefined, { isFreeText: true }));
  }
  const segments = sourcesFirst ? orderSegments(givenSegments, searching, query) : givenSegments;
  const recency = new Map(recentIds.map((id, index) => [id, index]));
  if (searching) {
    for (const segment of segments) {
      rows.push(
        ...(segment.ranking === "client"
          ? rankSegment(segment, query, recency, seen)
          : unrankedSegment(segment, seen)),
      );
    }
    return rows;
  }
  // Sources that lead stay ahead of the recently picked items too.
  const leading = sourcesFirst ? segments.filter((segment) => segment.origin === "source") : [];
  for (const segment of leading) rows.push(...unrankedSegment(segment, seen));
  const rest = segments.filter((segment) => !leading.includes(segment));
  rows.push(...recentRows(rest, recency, seen));
  for (const segment of rest) rows.push(...unrankedSegment(segment, seen));
  return rows;
}

export function selectableIndices(rows: readonly QuickPickRow<unknown>[]): number[] {
  const indices: number[] = [];
  rows.forEach((row, index) => {
    // A disabled row shows its reason but the keyboard passes over it.
    if (row.kind === "item" && !row.item.disabledReason) indices.push(index);
  });
  return indices;
}

export type QuickPickNavigation =
  | { type: "next" }
  | { type: "previous" }
  | { type: "nextGroup" }
  | { type: "previousGroup" }
  | { type: "pageDown"; pageSize: number }
  | { type: "pageUp"; pageSize: number }
  | { type: "first" }
  | { type: "last" };

export interface QuickPickNavigationOptions {
  /** Next and previous wrap around at the ends; off by default, as in a menu. */
  loop?: boolean;
}

/**
 * Each row's group, as the index of the heading that names it: a heading owns
 * itself, an item the heading of its own group above it, and an ungrouped item -1.
 */
function owningHeadings(rows: readonly QuickPickRow<unknown>[]): number[] {
  let heading = -1;
  let key: string | undefined;
  return rows.map((row, index) => {
    if (row.kind === "heading") {
      heading = index;
      key = row.key;
      return index;
    }
    if (heading !== -1 && row.headingKey === key) return heading;
    heading = -1;
    return -1;
  });
}

/**
 * The first enabled row of the nearest group after (or before) the active row's
 * group that has one, or -1 when the active row is ungrouped or none is found.
 */
function adjacentGroupStart(
  rows: readonly QuickPickRow<unknown>[],
  selectable: readonly number[],
  activeIndex: number,
  direction: 1 | -1,
): number {
  const owners = owningHeadings(rows);
  const ownHeading = activeIndex === -1 ? -1 : owners[activeIndex];
  if (ownHeading === -1) return -1;
  const headings = rows.flatMap((row, index) => (row.kind === "heading" ? [index] : []));
  for (
    let position = headings.indexOf(ownHeading) + direction;
    position >= 0 && position < headings.length;
    position += direction
  ) {
    const first = selectable.find((index) => owners[index] === headings[position]);
    if (first !== undefined) return first;
  }
  return -1;
}

// Layout can report a fractional or NaN page size; a page is always at least one row.
function pageStep(pageSize: number): number {
  return Math.max(1, Math.floor(pageSize) || 1);
}

/**
 * The row index to activate: the current one when a move has nowhere to go,
 * or -1 when nothing is selectable.
 */
export function navigateQuickPick(
  rows: readonly QuickPickRow<unknown>[],
  activeIndex: number,
  navigation: QuickPickNavigation,
  { loop = false }: QuickPickNavigationOptions = {},
): number {
  const selectable = selectableIndices(rows);
  if (selectable.length === 0) return -1;
  const position = selectable.indexOf(activeIndex);
  const last = selectable.length - 1;
  // From a row that can't be picked, such as Show more while it loads, moves start
  // where that row sits rather than at the top.
  const firstAfter = selectable.findIndex((index) => index > activeIndex);
  const below = firstAfter === -1 ? selectable.length : firstAfter;
  const step = (direction: 1 | -1): number => {
    const target = position !== -1 ? position + direction : direction === 1 ? below : below - 1;
    if (target >= 0 && target <= last) return selectable[target];
    return loop ? selectable[direction === 1 ? 0 : last] : activeIndex;
  };
  switch (navigation.type) {
    case "first":
      return selectable[0];
    case "last":
      return selectable[last];
    case "next":
      return step(1);
    case "previous":
      return step(-1);
    case "nextGroup":
    case "previousGroup": {
      const direction = navigation.type === "nextGroup" ? 1 : -1;
      const groupStart = adjacentGroupStart(rows, selectable, activeIndex, direction);
      return groupStart !== -1 ? groupStart : step(direction);
    }
    case "pageDown":
      return selectable[
        Math.min(
          last,
          Math.max(position !== -1 ? position : below - 1, 0) + pageStep(navigation.pageSize),
        )
      ];
    case "pageUp":
      return selectable[
        Math.max(0, (position !== -1 ? position : below) - pageStep(navigation.pageSize))
      ];
  }
}

/**
 * Keeps the active item when it survives a results change; otherwise
 * falls back to the first enabled row, or the first row when all are disabled.
 */
export function reconcileActiveIndex(
  rows: readonly QuickPickRow<unknown>[],
  activeItemId: string | null,
): number {
  if (activeItemId !== null) {
    const index = rows.findIndex((row) => row.kind === "item" && row.item.id === activeItemId);
    if (index !== -1) return index;
  }
  const firstEnabled = rows.findIndex((row) => row.kind === "item" && !row.item.disabledReason);
  return firstEnabled !== -1 ? firstEnabled : rows.findIndex((row) => row.kind === "item");
}
