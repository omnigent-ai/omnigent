import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import type { ConversationsInfiniteData } from "./sessionListCache";

export type ScopeCacheData = ConversationsInfiniteData & { windowSize: number };

/** Replace the refreshed window; retain older history only beyond the refresh cap. */
export function refreshScopeWindow(
  current: ScopeCacheData | undefined,
  incoming: ConversationsPage,
  limit: number,
): ScopeCacheData {
  const windowSize =
    current?.windowSize ?? Math.max(limit, current?.pages.flatMap((page) => page.data).length ?? 0);
  if (windowSize <= limit || !incoming.has_more) {
    return { pages: [incoming], pageParams: [undefined], windowSize };
  }
  const previous = current?.pages.flatMap((page) => page.data) ?? [];
  const incomingTail = incoming.data.at(-1);
  const older = incomingTail
    ? previous.filter((row) => compareSessionRows(row, incomingTail) > 0)
    : previous;
  const rows = dedupeSessionRows([...older, ...incoming.data]).sort(compareSessionRows);
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
): ScopeCacheData {
  const rows = dedupeSessionRows([
    ...current.pages.flatMap((page) => page.data),
    ...incoming.data,
  ]).sort(compareSessionRows);
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

export function compareSessionRows(a: Conversation, b: Conversation): number {
  return b.updated_at - a.updated_at || b.id.localeCompare(a.id);
}

/** Only the merged prefix above both unfinished scopes' tails is complete. */
export function mergeScopeRows(
  mine: Conversation[],
  shared: Conversation[],
  mineHasMore: boolean,
  sharedHasMore: boolean,
  mineCursor?: string | null,
  sharedCursor?: string | null,
): { rows: Conversation[]; watermark: Conversation | undefined } {
  const mineTail = mine.find((row) => row.id === mineCursor) ?? mine.at(-1);
  const sharedTail = shared.find((row) => row.id === sharedCursor) ?? shared.at(-1);
  const tails = [mineHasMore ? mineTail : undefined, sharedHasMore ? sharedTail : undefined]
    .filter((row): row is Conversation => row !== undefined)
    .sort(compareSessionRows);
  const watermark = tails[0];
  const rows = dedupeSessionRows([...mine, ...shared]).sort(compareSessionRows);
  return {
    rows: watermark ? rows.filter((row) => compareSessionRows(row, watermark) <= 0) : rows,
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
