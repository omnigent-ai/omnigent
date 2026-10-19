import type { Conversation, ConversationsPage, SessionSortBy } from "@/hooks/useConversations";
import type { ConversationsInfiniteData } from "./sessionListCache";

export type ScopeCacheData = ConversationsInfiniteData & { windowSize: number };

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
  a: Conversation,
  b: Conversation,
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
): { rows: Conversation[]; watermark: Conversation | undefined } {
  const compare = (a: Conversation, b: Conversation) => compareSessionRows(a, b, sortBy);
  const mineTail = mine.find((row) => row.id === mineCursor) ?? mine.at(-1);
  const sharedTail = shared.find((row) => row.id === sharedCursor) ?? shared.at(-1);
  const tails = [mineHasMore ? mineTail : undefined, sharedHasMore ? sharedTail : undefined]
    .filter((row): row is Conversation => row !== undefined)
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
