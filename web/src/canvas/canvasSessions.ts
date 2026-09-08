// Session loading for the Canvas page. The sidebar pages through the list 30
// rows at a time, which is right for a scrolling list but makes a canvas of
// several hundred sessions take dozens of sequential requests to fill. The
// canvas instead paints a preview from the sidebar's cache at once, then loads
// the canonical list in 1,000-row pages, and refreshes on a timer and on focus.

import { useCallback, useEffect, useRef, useState } from "react";
import { type InfiniteData, type QueryClient, useQueryClient } from "@tanstack/react-query";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import { authenticatedFetch } from "@/lib/identity";
import { dedupeConversationsById } from "@/shell/sidebarNav";

/** First page when nothing is cached: small, so the first paint is quick. */
export const INITIAL_SESSION_PAGE_LIMIT = 25;
/** The server's maximum page size. */
export const SESSION_PAGE_LIMIT = 1_000;
export const MAX_SESSION_PAGES = 200;
export const MAX_SESSIONS = 5_000;
export const SESSION_POLL_INTERVAL_MS = 30_000;

export interface SessionLoadProgress {
  sessions: Conversation[];
  hasMore: boolean;
}

export interface CanvasSessions {
  sessions: Conversation[];
  /** True once the first page (or the cached preview) is on screen. */
  loaded: boolean;
  /** True while the first full load is still fetching pages. */
  loadingMore: boolean;
  /** True once a full canonical load has finished at least once. */
  complete: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function isTopLevelActive(session: Conversation): boolean {
  return !session.archived && session.parent_session_id == null;
}

export function sessionListQuery(after: string | null, limit: number): string {
  const query = new URLSearchParams();
  query.set("limit", String(limit));
  query.set("sort_by", "updated_at");
  query.set("order", "desc");
  query.set("kind", "default");
  query.set("include_archived", "false");
  if (after) query.set("after", after);
  return query.toString();
}

/** Top-level, non-archived rows already in the sidebar's list cache, newest first. */
export function cachedSessionPreview(
  queryClient: QueryClient,
  limit: number = INITIAL_SESSION_PAGE_LIMIT,
): Conversation[] {
  const rows = queryClient
    .getQueriesData<InfiniteData<ConversationsPage, string | undefined>>({
      queryKey: ["conversations"],
    })
    .flatMap(([, data]) => data?.pages.flatMap((page) => page.data) ?? []);
  return dedupeConversationsById(rows.filter(isTopLevelActive))
    .sort((left, right) => right.updated_at - left.updated_at || left.id.localeCompare(right.id))
    .slice(0, limit);
}

async function fetchSessionPage(
  after: string | null,
  limit: number,
  signal?: AbortSignal,
): Promise<ConversationsPage> {
  const response = await authenticatedFetch(`/v1/sessions?${sessionListQuery(after, limit)}`, {
    signal,
  });
  if (!response.ok) throw new Error(`Session list request failed (${response.status})`);
  return (await response.json()) as ConversationsPage;
}

/**
 * Load every top-level session, newest first. The first page is a quick 25
 * rows unless a preview already covers the first paint; later pages take the
 * server maximum. `onProgress` fires after each page; while more pages are
 * pending the preview stays merged in so cards never vanish mid-load. Only the
 * completed server pages define the final membership.
 */
export async function loadAllSessions(
  preview: readonly Conversation[],
  onProgress: (progress: SessionLoadProgress) => void,
  signal?: AbortSignal,
): Promise<Conversation[]> {
  const previewById = new Map(preview.map((session) => [session.id, session]));
  const loaded = new Map<string, Conversation>();
  const seenCursors = new Set<string>();
  let after: string | null = null;
  for (let pageNumber = 0; pageNumber < MAX_SESSION_PAGES; pageNumber += 1) {
    const limit =
      pageNumber === 0 && preview.length === 0 ? INITIAL_SESSION_PAGE_LIMIT : SESSION_PAGE_LIMIT;
    // Each page's cursor comes from the previous response, so pages are sequential.
    // oxlint-disable-next-line no-await-in-loop
    const page = await fetchSessionPage(after, limit, signal);
    for (const session of page.data) {
      if (isTopLevelActive(session)) loaded.set(session.id, session);
    }
    if (loaded.size > MAX_SESSIONS) {
      throw new Error(`Canvas session load exceeded ${MAX_SESSIONS} sessions`);
    }
    const visible = page.has_more ? new Map([...previewById, ...loaded]) : loaded;
    onProgress({ sessions: [...visible.values()], hasMore: page.has_more });
    if (!page.has_more) return [...loaded.values()];
    if (!page.last_id) throw new Error("Canvas session load received no next cursor");
    if (seenCursors.has(page.last_id)) {
      throw new Error("Canvas session load received a repeated cursor");
    }
    seenCursors.add(page.last_id);
    after = page.last_id;
  }
  throw new Error(`Canvas session load exceeded ${MAX_SESSION_PAGES} pages`);
}

/** Loaded rows first; cards already on the canvas stay until a full load replaces them. */
function mergePartial(existing: readonly Conversation[], loaded: Conversation[]): Conversation[] {
  const loadedIds = new Set(loaded.map((session) => session.id));
  return [...loaded, ...existing.filter((session) => !loadedIds.has(session.id))];
}

export function useCanvasSessions(): CanvasSessions {
  const queryClient = useQueryClient();
  const [state, setState] = useState(() => {
    const preview = cachedSessionPreview(queryClient);
    return {
      sessions: preview,
      loaded: preview.length > 0,
      loadingMore: false,
      complete: false,
      error: null as string | null,
    };
  });
  const sessionsRef = useRef(state.sessions);
  const completeRef = useRef(false);
  const inFlightRef = useRef<Promise<void> | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const refresh = useCallback((): Promise<void> => {
    if (inFlightRef.current) return inFlightRef.current;
    const existing = sessionsRef.current;
    // Once the full list is known, routine refreshes stay quiet.
    if (!completeRef.current) {
      setState((current) => ({ ...current, loadingMore: true }));
    }
    const request = (async () => {
      try {
        await loadAllSessions(existing, (progress) => {
          if (!aliveRef.current) return;
          const sessions = progress.hasMore
            ? mergePartial(existing, progress.sessions)
            : progress.sessions;
          sessionsRef.current = sessions;
          if (!progress.hasMore) completeRef.current = true;
          setState((current) => ({
            ...current,
            sessions,
            loaded: true,
            complete: current.complete || !progress.hasMore,
            error: null,
          }));
        });
      } catch (reason) {
        if (aliveRef.current) {
          setState((current) => ({
            ...current,
            error: reason instanceof Error ? reason.message : "Could not load sessions",
          }));
        }
      } finally {
        if (aliveRef.current) {
          setState((current) => ({ ...current, loaded: true, loadingMore: false }));
        }
      }
    })();
    inFlightRef.current = request;
    void request.finally(() => {
      if (inFlightRef.current === request) inFlightRef.current = null;
    });
    return request;
  }, []);

  // Initial load, then poll like the sidebar does and catch up on window focus.
  useEffect(() => {
    void refresh();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const refreshIfVisible = () => {
      if (!document.hidden) void refresh();
    };
    const schedule = () => {
      timer = setTimeout(async () => {
        if (!document.hidden) await refresh();
        if (!cancelled) schedule();
      }, SESSION_POLL_INTERVAL_MS);
    };
    schedule();
    window.addEventListener("focus", refreshIfVisible);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      window.removeEventListener("focus", refreshIfVisible);
    };
  }, [refresh]);

  return { ...state, refresh };
}
