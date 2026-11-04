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

/** Code points the server keeps of its stored ``sort_by=title`` key. */
const TITLE_SORT_KEY_MAX_CHARS = 512;

/**
 * The server's ``sort_by=title`` key: ASCII case folded, untitled last. The
 * server folds only ASCII on every database, so this must not use toLowerCase.
 */
export function titleSortKey(title: string | null | undefined): string {
  if (!title) return "1";
  const key = `0${title.replace(/[A-Z]/g, (letter) => letter.toLowerCase())}`;
  return key.length > TITLE_SORT_KEY_MAX_CHARS
    ? Array.from(key).slice(0, TITLE_SORT_KEY_MAX_CHARS).join("")
    : key;
}

/** Orders like the server's binary collations: by code point, not UTF-16 unit. */
export function compareCodePoints(a: string, b: string): number {
  const length = Math.min(a.length, b.length);
  for (let index = 0; index < length; index++) {
    if (a.charCodeAt(index) !== b.charCodeAt(index)) {
      return (a.codePointAt(index) ?? 0) - (b.codePointAt(index) ?? 0);
    }
  }
  return a.length - b.length;
}

export function compareSessionRows(
  a: Conversation,
  b: Conversation,
  sortBy: SessionSortBy = "updated_at",
): number {
  if (sortBy === "title") {
    return (
      compareCodePoints(titleSortKey(a.title), titleSortKey(b.title)) ||
      compareCodePoints(a.id, b.id)
    );
  }
  return b[sortBy] - a[sortBy] || b.id.localeCompare(a.id);
}

const TITLE_CURSOR_PREFIX = "title.";

/**
 * A row that sorts where a page's cursor continues. A ``sort_by=title`` token
 * is ``"title."`` plus base64url JSON of ``[title_sort_key, id]``.
 */
function scopeTail(rows: Conversation[], cursor: string | null | undefined) {
  if (!cursor?.startsWith(TITLE_CURSOR_PREFIX)) {
    return rows.find((row) => row.id === cursor) ?? rows.at(-1);
  }
  try {
    const base64 = cursor.slice(TITLE_CURSOR_PREFIX.length).replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
    const [key, id] = JSON.parse(new TextDecoder().decode(bytes)) as unknown[];
    const base = rows.find((row) => row.id === id) ?? rows.at(-1);
    if (typeof key !== "string" || typeof id !== "string") return base;
    // The key is already folded, so titleSortKey(title) reproduces the token's position.
    return { ...base, id, title: key.slice(1) } as Conversation;
  } catch {
    return rows.at(-1);
  }
}

/**
 * Only the merged prefix above both unfinished scopes' tails is complete.
 * Under Name, `watermark` is a position: only its `id` and folded `title` are meaningful.
 */
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
  const mineTail = scopeTail(mine, mineCursor);
  const sharedTail = scopeTail(shared, sharedCursor);
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
