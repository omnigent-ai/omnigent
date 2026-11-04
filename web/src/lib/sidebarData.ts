import type { Conversation, ConversationsPage, SessionSortBy } from "@/hooks/useConversations";
import type { ConversationsInfiniteData } from "./sessionListCache";

/** The cursor row's sort key as of the fetch that returned it. */
export type ScopeBoundary = Pick<Conversation, "id" | "updated_at" | "created_at">;

// `boundary` is the next page's cursor as it sorted when fetched. Pushed updates
// move rows in place, so the live cursor row can sort higher than the point the
// server stopped at; the merge must keep using the fetched position.
export type ScopeCacheData = ConversationsInfiniteData & {
  windowSize: number;
  boundary?: ScopeBoundary;
};

function boundaryOf(page: ConversationsPage): ScopeBoundary | undefined {
  const row = page.data.find((session) => session.id === page.last_id);
  return row && { id: row.id, updated_at: row.updated_at, created_at: row.created_at };
}

/** Replace the refreshed window; retain older history only beyond the refresh cap. */
export function refreshScopeWindow(
  current: ScopeCacheData | undefined,
  incoming: ConversationsPage,
  limit: number,
  sortBy: SessionSortBy = "updated_at",
): ScopeCacheData {
  const compare = (a: Conversation, b: Conversation) => compareSessionRows(a, b, sortBy);
  const windowSize =
    current?.windowSize ?? Math.max(limit, current?.pages.flatMap((page) => page.data).length ?? 0);
  if (windowSize <= limit || !incoming.has_more) {
    // The server breaks timestamp ties differently; sort so a refresh cannot
    // undo the order appendScopePage produced. The cursor stays the server's.
    const rows = [...incoming.data].sort(compare);
    return {
      pages: [{ ...incoming, data: rows, first_id: rows[0]?.id ?? null }],
      pageParams: [undefined],
      windowSize,
      boundary: boundaryOf(incoming),
    };
  }
  const previous = current?.pages.flatMap((page) => page.data) ?? [];
  const incomingTail = incoming.data.at(-1);
  const older = incomingTail ? previous.filter((row) => compare(row, incomingTail) > 0) : previous;
  const rows = dedupeSessionRows([...older, ...incoming.data]).sort(compare);
  const overlaps = incomingTail && previous.some((row) => row.id === incomingTail.id);
  const cursor = overlaps ? current?.pages.at(-1) : incoming;
  return {
    pages: [
      {
        ...incoming,
        data: rows,
        first_id: rows[0]?.id ?? null,
        last_id: cursor?.last_id ?? null,
        has_more: cursor?.has_more ?? false,
      },
    ],
    pageParams: [undefined],
    windowSize,
    boundary: overlaps ? current?.boundary : boundaryOf(incoming),
  };
}

export function appendScopePage(
  current: ScopeCacheData,
  incoming: ConversationsPage,
  pageSize: number,
  sortBy: SessionSortBy = "updated_at",
): ScopeCacheData {
  const rows = dedupeSessionRows([
    ...current.pages.flatMap((page) => page.data),
    ...incoming.data,
  ]).sort((a, b) => compareSessionRows(a, b, sortBy));
  return {
    pages: [{ ...incoming, data: rows, first_id: rows[0]?.id ?? null }],
    pageParams: [undefined],
    windowSize:
      (current.windowSize ?? current.pages.flatMap((page) => page.data).length) + pageSize,
    boundary: boundaryOf(incoming),
  };
}

export function dedupeSessionRows(rows: Conversation[]): Conversation[] {
  const byId = new Map<string, Conversation>();
  for (const row of rows) {
    const previous = byId.get(row.id);
    if (!previous || row.updated_at >= previous.updated_at) byId.set(row.id, row);
  }
  return [...byId.values()];
}

export function compareSessionRows(
  a: ScopeBoundary,
  b: ScopeBoundary,
  sortBy: SessionSortBy = "updated_at",
): number {
  return b[sortBy] - a[sortBy] || b.id.localeCompare(a.id);
}

/** Only the merged prefix above both unfinished scopes' tails is complete. */
export function mergeScopeRows(
  mine: Conversation[],
  shared: Conversation[],
  mineHasMore: boolean,
  sharedHasMore: boolean,
  mineCursor?: string | null,
  sharedCursor?: string | null,
  sortBy: SessionSortBy = "updated_at",
  mineBoundary?: ScopeBoundary,
  sharedBoundary?: ScopeBoundary,
): { rows: Conversation[]; watermark: ScopeBoundary | undefined } {
  const compare = (a: ScopeBoundary, b: ScopeBoundary) => compareSessionRows(a, b, sortBy);
  // Prefer the cursor as fetched: a pushed bump can lift the live cursor row,
  // and a watermark taken from it would hide every row below.
  const tailOf = (
    rows: Conversation[],
    cursor?: string | null,
    boundary?: ScopeBoundary,
  ): ScopeBoundary | undefined =>
    boundary && boundary.id === cursor
      ? boundary
      : (rows.find((row) => row.id === cursor) ?? rows.at(-1));
  const mineTail = tailOf(mine, mineCursor, mineBoundary);
  const sharedTail = tailOf(shared, sharedCursor, sharedBoundary);
  const tails = [mineHasMore ? mineTail : undefined, sharedHasMore ? sharedTail : undefined]
    .filter((row): row is ScopeBoundary => row !== undefined)
    .sort(compare);
  const watermark = tails[0];
  const rows = dedupeSessionRows([...mine, ...shared]).sort(compare);
  return {
    rows: watermark ? rows.filter((row) => compare(row, watermark) <= 0) : rows,
    watermark,
  };
}

export function sessionRowsPage(rows: Conversation[], hasMore = false): ConversationsPage {
  return {
    data: rows,
    first_id: rows[0]?.id ?? null,
    last_id: rows.at(-1)?.id ?? null,
    has_more: hasMore,
  };
}
