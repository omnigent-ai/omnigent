// A session's journal for the Activity tab: one page of the newest entries,
// older pages only on request, kept live by `session.journal.appended` and
// caught up when the stream reopens. Every read is one bounded page; when
// older pages exist, the newest plan and statuses are read too, so every step
// of the plan shows where it stands.

import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useSyncExternalStore } from "react";
import {
  fetchJournalPage,
  mergeJournalEntries,
  sessionJournalKey,
  type JournalEntry,
  type JournalPage,
} from "@/lib/sessionJournal";
import { isSessionStreamOpen, subscribeSessionStreams } from "@/lib/sessionStreamSignals";
import { isTempConvId } from "@/lib/tempConversationId";

const PAGE_SIZE = "100";
// Enough statuses to cover every step of a plan, which has at most 100.
const STATUS_PAGE_SIZE = "1000";
/** How often the tab catches up while the session's stream is down. */
export const JOURNAL_FALLBACK_REFETCH_MS = 30_000;
// A catch-up reads at most this many pages; past that the tab refetches instead.
const CATCH_UP_MAX_PAGES = 5;

type JournalData = InfiniteData<JournalPage, string | null>;

function newestEntry(data: JournalData | undefined): JournalEntry | undefined {
  let newest: JournalEntry | undefined;
  for (const page of data?.pages ?? []) {
    for (const entry of page.data) {
      if (!newest || entry.position > newest.position) newest = entry;
    }
  }
  return newest;
}

function withEntries(data: JournalData, entries: JournalEntry[]): JournalData {
  const known = new Set(data.pages.flatMap((page) => page.data.map((entry) => entry.id)));
  const fresh = entries.filter((entry) => !known.has(entry.id));
  if (fresh.length === 0) return data;
  const [first, ...rest] = data.pages;
  // Pages are newest first, so new entries go to the front of the first page.
  const data0 = mergeJournalEntries(first.data, fresh).reverse();
  return { ...data, pages: [{ ...first, data: data0 }, ...rest] };
}

/** Start over from the newest page, dropping older pages the user had loaded. */
function reloadNewestPage(queryClient: QueryClient, conversationId: string): Promise<void> {
  return queryClient.resetQueries({ queryKey: sessionJournalKey(conversationId) });
}

/** Add a live entry to the session's cached journal, once. */
export function applyJournalEntry(
  queryClient: QueryClient,
  conversationId: string,
  entry: JournalEntry,
): void {
  const key = sessionJournalKey(conversationId);
  const data = queryClient.getQueryData<JournalData>(key);
  if (!data || data.pages.length === 0) return;
  queryClient.setQueryData<JournalData>(key, withEntries(data, [entry]));
}

/** Fetch what the cached journal missed, e.g. while the stream was down. */
export async function catchUpSessionJournal(
  queryClient: QueryClient,
  conversationId: string,
): Promise<void> {
  const key = sessionJournalKey(conversationId);
  const after = newestEntry(queryClient.getQueryData<JournalData>(key));
  if (!after) {
    await reloadNewestPage(queryClient, conversationId);
    return;
  }
  const entries: JournalEntry[] = [];
  let cursor = after.id;
  /* oxlint-disable no-await-in-loop -- each page needs the previous page's last entry */
  for (let page = 0; page < CATCH_UP_MAX_PAGES; page += 1) {
    const next = await fetchJournalPage(conversationId, {
      order: "asc",
      after: cursor,
      limit: PAGE_SIZE,
    });
    entries.push(...next.data);
    if (!next.has_more || !next.last_id) {
      const data = queryClient.getQueryData<JournalData>(key);
      if (data) queryClient.setQueryData<JournalData>(key, withEntries(data, entries));
      return;
    }
    cursor = next.last_id;
  }
  /* oxlint-enable no-await-in-loop */
  // Too far behind to merge: show the newest page again.
  await reloadNewestPage(queryClient, conversationId);
}

export interface SessionJournalView {
  /** Position order, oldest first. */
  entries: JournalEntry[];
  isLoading: boolean;
  error: Error | null;
  hasOlder: boolean;
  loadingOlder: boolean;
  loadOlder: () => void;
}

/**
 * The session's journal, newest page first, with older pages on demand.
 *
 * @param enabled - False on servers without journals, or while the tab is hidden.
 */
export function useSessionJournal(
  rawConversationId: string | undefined,
  enabled: boolean,
): SessionJournalView {
  const queryClient = useQueryClient();
  const conversationId =
    rawConversationId && !isTempConvId(rawConversationId) ? rawConversationId : undefined;
  const active = enabled && conversationId !== undefined;
  const query = useInfiniteQuery({
    queryKey: sessionJournalKey(conversationId),
    queryFn: ({ pageParam }) =>
      fetchJournalPage(conversationId!, {
        order: "desc",
        limit: PAGE_SIZE,
        ...(pageParam ? { after: pageParam } : {}),
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => (last.has_more && last.last_id ? last.last_id : undefined),
    enabled: active,
    // The session stream keeps it current; see the catch-up below.
    staleTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });

  const streamOpen = useSyncExternalStore(
    (notify) => subscribeSessionStreams(notify),
    () => (conversationId ? isSessionStreamOpen(conversationId) : false),
    () => false,
  );
  useEffect(() => {
    if (!active) return;
    return subscribeSessionStreams((id, reopened) => {
      if (id === conversationId && reopened) void catchUpSessionJournal(queryClient, id);
    });
  }, [active, conversationId, queryClient]);
  useEffect(() => {
    if (!active || streamOpen || query.status !== "success") return;
    const timer = window.setInterval(
      () => void catchUpSessionJournal(queryClient, conversationId!),
      JOURNAL_FALLBACK_REFETCH_MS,
    );
    return () => window.clearInterval(timer);
  }, [active, streamOpen, conversationId, queryClient, query.status]);

  // When older pages exist, the current plan and its steps' statuses may be
  // among them, so read those two kinds on their own. Shares the journal's key
  // prefix, so reloading the newest page resets it too.
  const olderExist = query.hasNextPage;
  const planAndStatuses = useQuery({
    queryKey: [...sessionJournalKey(conversationId), "plan-and-statuses"],
    queryFn: () =>
      Promise.all([
        fetchJournalPage(conversationId!, { kind: "plan", order: "desc", limit: "1" }),
        fetchJournalPage(conversationId!, {
          kind: "status",
          order: "desc",
          limit: STATUS_PAGE_SIZE,
        }),
      ]),
    enabled: active && olderExist,
    staleTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });

  const pages = query.data?.pages;
  const extraPages = olderExist ? planAndStatuses.data : undefined;
  const entries = useMemo(
    () =>
      mergeJournalEntries(
        ...(extraPages ?? []).map((page) => page.data),
        ...(pages ?? []).map((page) => page.data),
      ),
    [pages, extraPages],
  );
  return {
    entries,
    isLoading: active && (query.isPending || (olderExist && planAndStatuses.isPending)),
    error: query.error,
    hasOlder: query.hasNextPage,
    loadingOlder: query.isFetchingNextPage,
    loadOlder: () => void query.fetchNextPage(),
  };
}
