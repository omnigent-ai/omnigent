// The Pull Requests list as one column of fixed-height items, so only the rows near the
// viewport render: each group's header, then its rows. Rows and headers never wrap, so every
// item's offset follows from the list's width without measuring it.

/** Rollback switch: off, the list draws and reads every row, as before windowing. */
export const PULL_REQUEST_LIST_PAGES_FLAG = "pull-request-list-pages";

/** The items' heights at one list width, as `.pr-list-row` and `.pr-list-group-header` draw them. */
export interface ListMetrics {
  rowHeight: number;
  headerHeight: number;
}

/** The widest list that stacks a row's metadata beneath its title, as the CSS container query. */
export const NARROW_LIST_MAX_WIDTH = 620;
export const WIDE_LIST_METRICS: ListMetrics = { rowHeight: 36, headerHeight: 32 };
export const NARROW_LIST_METRICS: ListMetrics = { rowHeight: 52, headerHeight: 32 };
/** A wide list's row height, for placeholders that don't follow the width. */
export const LIST_ROW_HEIGHT = WIDE_LIST_METRICS.rowHeight;

/** The heights a list of content width `width` draws; a list not laid out yet reads as wide. */
export function listMetrics(width: number | undefined): ListMetrics {
  return width !== undefined && width > 0 && width <= NARROW_LIST_MAX_WIDTH
    ? NARROW_LIST_METRICS
    : WIDE_LIST_METRICS;
}

export type ListItem =
  { kind: "header"; groupId: string } | { kind: "row"; groupId: string; url: string };

/** One group as the list lays it out: whether it draws a header, and its shown rows. */
export interface ListGroupLayout {
  id: string;
  header: boolean;
  /** The rows the group shows, in order: none while it's collapsed. */
  urls: readonly string[];
}

export interface ListLayout {
  items: readonly ListItem[];
  /** Each item's top, from the first item. */
  offsets: readonly number[];
  /** Each row's item index, by URL. */
  rowIndex: ReadonlyMap<string, number>;
  totalHeight: number;
  /** The heights the offsets and spacers use. */
  metrics: ListMetrics;
}

export const itemHeight = (item: ListItem, metrics: ListMetrics) =>
  item.kind === "header" ? metrics.headerHeight : metrics.rowHeight;

export function listLayout(
  groups: readonly ListGroupLayout[],
  metrics: ListMetrics = WIDE_LIST_METRICS,
): ListLayout {
  const items: ListItem[] = [];
  const offsets: number[] = [];
  const rowIndex = new Map<string, number>();
  let offset = 0;
  const push = (item: ListItem) => {
    if (item.kind === "row") rowIndex.set(item.url, items.length);
    items.push(item);
    offsets.push(offset);
    offset += itemHeight(item, metrics);
  };
  for (const group of groups) {
    if (group.header) push({ kind: "header", groupId: group.id });
    for (const url of group.urls) push({ kind: "row", groupId: group.id, url });
  }
  return { items, offsets, rowIndex, totalHeight: offset, metrics };
}

/** A group's part of the rendered window: the rows drawn, and the space left for the others. */
export interface GroupSlice {
  /** Space for the groups left out between the previous drawn group and this one. */
  above: number;
  /** The group's rows inside the window, in order. */
  urls: readonly string[];
  /** Space for the group's rows left out right above each drawn row, by its place in `urls`. */
  spaceAbove: readonly number[];
  /** Space for the group's rows below its last drawn one. */
  after: number;
}

/**
 * What a window drawing the items `drawn` (indices in order, overscan and kept rows included)
 * draws: each group that has an item in it, with its header kept so a sticky header stays in
 * place, and the space for every item left out, so the list keeps its whole height.
 */
export function windowSlices(
  layout: ListLayout,
  groups: readonly ListGroupLayout[],
  drawn: readonly number[] | null,
): { slices: ReadonlyMap<string, GroupSlice>; before: number; after: number } {
  const slices = new Map<string, GroupSlice>();
  if (!drawn || drawn.length === 0 || layout.items.length === 0) {
    return { slices, before: 0, after: layout.totalHeight };
  }
  const { rowHeight, headerHeight } = layout.metrics;
  const drawnIndices = new Set(drawn);
  let index = 0;
  let before: number | null = null;
  // Space for the groups left out since the last drawn one.
  let skipped = 0;
  for (const group of groups) {
    const first = index;
    const rowsStart = first + (group.header ? 1 : 0);
    index = rowsStart + group.urls.length;
    const drawnRows: number[] = [];
    group.urls.forEach((_url, place) => {
      if (drawnIndices.has(rowsStart + place)) drawnRows.push(place);
    });
    if (!(group.header && drawnIndices.has(first)) && drawnRows.length === 0) {
      skipped += (group.header ? headerHeight : 0) + group.urls.length * rowHeight;
      continue;
    }
    if (before === null) before = skipped;
    const spaceAbove: number[] = [];
    let previous = -1;
    for (const place of drawnRows) {
      spaceAbove.push((place - previous - 1) * rowHeight);
      previous = place;
    }
    slices.set(group.id, {
      above: slices.size === 0 ? 0 : skipped,
      urls: drawnRows.map((place) => group.urls[place]!),
      spaceAbove,
      after: (group.urls.length - 1 - previous) * rowHeight,
    });
    skipped = 0;
  }
  return { slices, before: before ?? 0, after: skipped };
}

/** The rows whose items `[start, end]` hold (the viewport's own range, without overscan). */
export function rowsIn(layout: ListLayout, range: { start: number; end: number } | null) {
  if (!range) return [];
  const urls: string[] = [];
  for (let index = Math.max(0, range.start); index <= range.end; index++) {
    const item = layout.items[index];
    if (item?.kind === "row") urls.push(item.url);
  }
  return urls;
}

/**
 * The items a window draws: the viewport's with its overscan, as TanStack Virtual's default
 * does, plus `kept` wherever it is, so the keyboard's active row stays mounted off screen.
 */
export function drawnItems(
  range: { startIndex: number; endIndex: number; overscan: number; count: number },
  kept: number | undefined,
): number[] {
  const start = Math.max(range.startIndex - range.overscan, 0);
  const end = Math.min(range.endIndex + range.overscan, range.count - 1);
  const indices: number[] = [];
  for (let index = start; index <= end; index++) indices.push(index);
  if (kept === undefined || kept < 0 || kept >= range.count) return indices;
  if (kept < start) return [kept, ...indices];
  if (kept > end) return [...indices, kept];
  return indices;
}
