// Pure presentation for QuickPick: turns segments of items and the query into
// the ordered rows the list renders, plus keyboard navigation over them.

import type { MatchRange } from "./match";
import {
  compareItemMatches,
  type FuzzyMatching,
  itemQueryTokens,
  type ItemMatch,
  matchItem,
} from "./itemRanking";
import type {
  QuickPickHeadingRow,
  QuickPickItem,
  QuickPickItemRow,
  QuickPickRanking,
  QuickPickRow,
} from "./model";

/** One run of items that rank together, such as commands or one group of search results. */
export interface QuickPickSegment<Value> {
  sourceId: string;
  ranking: QuickPickRanking;
  /** Defaults to "fuzzy". */
  fuzzyMatching?: FuzzyMatching;
  items: readonly QuickPickItem<Value>[];
}

export interface QuickPickPresentationInput<Value> {
  segments: readonly QuickPickSegment<Value>[];
  query: string;
}

const NO_RANGES: readonly MatchRange[] = [];

function itemRow<Value>(
  item: QuickPickItem<Value>,
  ranges?: Record<string, MatchRange[]>,
): QuickPickItemRow<Value> {
  return {
    kind: "item",
    key: `item:${item.id}`,
    item,
    labelRanges: ranges?.label ?? item.highlights?.label ?? NO_RANGES,
    descriptionRanges: ranges?.description ?? item.highlights?.description ?? NO_RANGES,
  };
}

function headingRow(sourceId: string, label: string): QuickPickHeadingRow {
  return { kind: "heading", key: `heading:${sourceId}:${label}`, label };
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
    const groupRows: QuickPickRow<Value>[] = [];
    for (const item of itemsByGroup.get(group) ?? []) {
      const row = rowsByItem.get(item);
      if (!row || seen.has(item.id)) continue;
      seen.add(item.id);
      groupRows.push(row);
    }
    if (groupRows.length === 0) continue;
    if (group !== undefined) rows.push(headingRow(sourceId, group));
    rows.push(...groupRows);
  }
  return rows;
}

interface RankedItem<Value> {
  item: QuickPickItem<Value>;
  match: ItemMatch;
  sourceIndex: number;
}

function rankSegment<Value>(
  segment: QuickPickSegment<Value>,
  query: string,
  seen: Set<string>,
): QuickPickRow<Value>[] {
  const tokens = itemQueryTokens(query);
  const ranked: RankedItem<Value>[] = [];
  segment.items.forEach((item, sourceIndex) => {
    const match = matchItem(tokens, item, segment.fuzzyMatching);
    if (match) ranked.push({ item, match, sourceIndex });
  });
  ranked.sort(
    (first, second) =>
      compareItemMatches(first.match, second.match) || first.sourceIndex - second.sourceIndex,
  );
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

/**
 * Without a query, every segment in source order. With one, client-ranked
 * segments keep only matching items, best first within each group.
 */
export function buildQuickPickRows<Value>({
  segments,
  query,
}: QuickPickPresentationInput<Value>): QuickPickRow<Value>[] {
  const searching = query.trim().length > 0;
  // The first row for an ID wins, such as a session two searches both return.
  const seen = new Set<string>();
  return segments.flatMap((segment) =>
    searching && segment.ranking === "client"
      ? rankSegment(segment, query, seen)
      : unrankedSegment(segment, seen),
  );
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
  let label: string | undefined;
  return rows.map((row, index) => {
    if (row.kind === "heading") {
      heading = index;
      label = row.label;
      return index;
    }
    if (heading !== -1 && row.item.group === label) return heading;
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
  const step = (direction: 1 | -1): number => {
    if (position === -1) return direction === 1 ? selectable[0] : activeIndex;
    const target = position + direction;
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
      return selectable[Math.min(last, Math.max(position, 0) + navigation.pageSize)];
    case "pageUp":
      return selectable[Math.max(0, position - navigation.pageSize)];
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
