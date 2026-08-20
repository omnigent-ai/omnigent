import { useInfiniteQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/**
 * One conversation item exactly as the server serializes it, with no
 * mapping or normalization applied. The execution-logs panel renders
 * each item as raw JSON (parity with the TUI's Ctrl+O view), so the
 * UI consumes the wire shape directly rather than the narrowed types
 * in ``lib/conversationItems``.
 */
export type RawSessionItem = Record<string, unknown>;

interface ItemsResponse {
  object: "list";
  data: RawSessionItem[];
  first_id: string | null;
  last_id: string | null;
  has_more: boolean;
}

/**
 * Per-page size for the execution-logs panel's infinite-scroll
 * loader. 50 keeps the initial fetch quick on long sessions while
 * still loading enough rows to fill the viewport without an
 * immediate second fetch.
 */
const ITEMS_PAGE_SIZE = 50;

/**
 * TanStack Query key for a session's raw items as consumed by the
 * execution-logs panel. Kept distinct from the chat scroll loader's
 * cursor-paginated key so the two callers don't fight over cache
 * invalidation.
 */
export function sessionItemsQueryKey(sessionId: string): readonly unknown[] {
  return ["session", sessionId, "items", "raw"];
}

interface UseSessionItemsResult {
  /** All items loaded across pages so far, in chronological order. */
  items: RawSessionItem[];
  isLoading: boolean;
  error: Error | null;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
}

/**
 * Fetch one page of items for a session via
 * ``GET /v1/sessions/{id}/items``. Exported for unit testing.
 *
 * Pages are ordered ``asc`` (oldest-first) so the panel's "#N"
 * numbering matches the user's "turn N" mental model — index 1
 * pins to the first item in the session.
 */
export async function fetchSessionItemsPage(
  sessionId: string,
  after: string | undefined,
): Promise<ItemsResponse> {
  const params = new URLSearchParams({
    limit: String(ITEMS_PAGE_SIZE),
    order: "asc",
  });
  if (after) params.set("after", after);
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/items?${params}`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as ItemsResponse;
}

/**
 * Live items for a session in raw JSON form, paginated for
 * infinite-scroll consumption by the execution-logs panel.
 *
 * The hook stays cold-friendly (long staleTime, no refetch on
 * mount) so re-opening the panel doesn't re-issue the first page.
 *
 * :param sessionId: Session/conversation identifier, or ``null``
 *     to disable the query.
 * :param pollMs: Optional poll interval in milliseconds. When set,
 *     the query refetches every ``pollMs`` ms (and pauses while the
 *     tab is backgrounded), so a panel that stays open shows new
 *     items as they land — useful for live debugging. Pass nothing
 *     (or ``null``) to keep the query a one-shot.
 */
export function useSessionItems(
  sessionId: string | null,
  pollMs?: number | null,
): UseSessionItemsResult {
  const { data, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useInfiniteQuery({
      queryKey:
        sessionId === null ? ["session", null, "items", "raw"] : sessionItemsQueryKey(sessionId),
      queryFn: ({ pageParam }) => fetchSessionItemsPage(sessionId as string, pageParam),
      initialPageParam: undefined as string | undefined,
      getNextPageParam: (lastPage) =>
        // The server's PaginatedList contract: `has_more` is the
        // authoritative "more rows exist" signal; `last_id` is the
        // cursor for the next page. Stop when either is absent.
        lastPage.has_more && lastPage.last_id ? lastPage.last_id : undefined,
      enabled: sessionId !== null,
      staleTime: 60_000,
      retry: false,
      refetchOnMount: false,
      // TanStack refetches every loaded page on this interval, so new
      // items land in the (formerly-empty-tail) last page on the next
      // tick. ``false`` (the default when undefined/null) disables.
      refetchInterval: pollMs ?? false,
    });
  const items = data ? data.pages.flatMap((p) => p.data) : [];
  return {
    items,
    isLoading,
    error: (error as Error | null) ?? null,
    hasNextPage: Boolean(hasNextPage),
    isFetchingNextPage,
    fetchNextPage: () => {
      void fetchNextPage();
    },
  };
}
