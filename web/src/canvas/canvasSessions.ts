// Session loading for the Canvas page. The sidebar pages through the list 30
// rows at a time, which is right for a scrolling list but makes a canvas of
// several hundred sessions take dozens of sequential requests to fill. The
// canvas instead paints a preview from the sidebar's cache at once, then loads
// the canonical list in 1,000-row pages, and refreshes on a timer and on focus.
// Between refreshes it mirrors the sidebar's list cache, which the
// `WS /v1/sessions/updates` stream patches in place, so a card's status,
// title, and unread state change the moment the sidebar row does.

import { useCallback, useEffect, useRef, useState } from "react";
import { type InfiniteData, type QueryClient, useQueryClient } from "@tanstack/react-query";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import { getOmnigentServerIdentity } from "@/lib/host";
import { authenticatedFetch, getCurrentUserId, resolveIdentity } from "@/lib/identity";
import { dedupeConversationsById } from "@/shell/sidebarNav";

/** First page when nothing is cached: small, so the first paint is quick. */
export const INITIAL_SESSION_PAGE_LIMIT = 25;
/** The server's maximum page size. */
export const SESSION_PAGE_LIMIT = 1_000;
export const MAX_SESSION_PAGES = 200;
export const MAX_SESSIONS = 5_000;
export const SESSION_POLL_INTERVAL_MS = 30_000;
const SESSION_CACHE_VERSION = 1;
const SESSION_CACHE_KEY_PREFIX = "omnigent:canvas-sessions";

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
  /** True only after this page has confirmed the complete list with the server. */
  networkConfirmed: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

// QueryClient survives route changes; scoping the remembered list to it keeps
// separate app roots and tests isolated while making revisits instantaneous.
interface RememberedCanvasSessions {
  serverId: string;
  viewerId: string;
  sessions: Conversation[];
  storedSignature: string | null;
}

const completeSessionsByClient = new WeakMap<QueryClient, RememberedCanvasSessions>();

interface StoredCanvasSessions {
  version: number;
  sessions: Conversation[];
}

function currentServerId(): string {
  return getOmnigentServerIdentity() ?? "default";
}

function sessionCachePrefix(serverId: string = currentServerId()): string {
  return `${SESSION_CACHE_KEY_PREFIX}:${serverId}`;
}

function sessionCacheKey(viewerId: string, serverId: string = currentServerId()): string {
  return `${sessionCachePrefix(serverId)}:${viewerId}`;
}

function hasStoredSessionsForServer(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const prefix = `${sessionCachePrefix()}:`;
    return Array.from({ length: window.sessionStorage.length }, (_, index) =>
      window.sessionStorage.key(index),
    ).some((key) => key?.startsWith(prefix));
  } catch {
    return false;
  }
}

function isStoredConversation(value: unknown): value is Conversation {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Partial<Conversation>;
  return (
    typeof row.id === "string" &&
    typeof row.updated_at === "number" &&
    Number.isFinite(row.updated_at) &&
    (row.status === undefined || typeof row.status === "string")
  );
}

function readStoredSessions(
  viewerId: string,
  serverId: string = currentServerId(),
): Conversation[] | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.sessionStorage.getItem(sessionCacheKey(viewerId, serverId));
    if (!raw) return undefined;
    const stored = JSON.parse(raw) as Partial<StoredCanvasSessions> | null;
    if (
      !stored ||
      stored.version !== SESSION_CACHE_VERSION ||
      !Array.isArray(stored.sessions) ||
      stored.sessions.length > MAX_SESSIONS ||
      !stored.sessions.every(isStoredConversation)
    ) {
      return undefined;
    }
    return dedupeConversationsById(stored.sessions.filter(isTopLevelActive));
  } catch {
    return undefined;
  }
}

function sessionSignature(sessions: readonly Conversation[]): string {
  return sessions
    .map((row) =>
      [
        row.id,
        row.updated_at,
        row.status,
        row.title,
        row.pending_elicitations_count,
        row.git_branch,
        row.project_id,
        row.workspace,
        row.archived,
        row.parent_session_id,
        row.owner,
        row.permission_level,
        row.labels?.omni_project,
      ].join("\0"),
    )
    .join("\x01");
}

function rememberedSessions(
  queryClient: QueryClient,
  viewerId: string | null,
): Conversation[] | undefined {
  if (viewerId === null) return undefined;
  const serverId = currentServerId();
  const remembered = completeSessionsByClient.get(queryClient);
  if (remembered?.serverId === serverId && remembered.viewerId === viewerId) {
    return remembered.sessions;
  }
  const stored = readStoredSessions(viewerId, serverId);
  if (stored) {
    const signature = sessionSignature(stored);
    completeSessionsByClient.set(queryClient, {
      serverId,
      viewerId,
      sessions: stored,
      storedSignature: signature,
    });
  }
  return stored;
}

function rememberCompleteSessions(
  queryClient: QueryClient,
  serverId: string,
  viewerId: string,
  sessions: Conversation[],
): void {
  const signature = sessionSignature(sessions);
  const previous = completeSessionsByClient.get(queryClient);
  const storedSignature =
    previous?.serverId === serverId && previous.viewerId === viewerId
      ? previous.storedSignature
      : null;
  completeSessionsByClient.set(queryClient, { serverId, viewerId, sessions, storedSignature });
  if (storedSignature === signature) return;
  if (typeof window === "undefined") return;
  try {
    const stored: StoredCanvasSessions = { version: SESSION_CACHE_VERSION, sessions };
    window.sessionStorage.setItem(sessionCacheKey(viewerId, serverId), JSON.stringify(stored));
    completeSessionsByClient.set(queryClient, {
      serverId,
      viewerId,
      sessions,
      storedSignature: signature,
    });
  } catch {
    // Memory caching still works when storage is disabled or full.
  }
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

/** The fields a card renders or files by; anything else changing is not worth a re-render. */
function sameCard(left: Conversation, right: Conversation): boolean {
  return (
    left.status === right.status &&
    left.title === right.title &&
    left.updated_at === right.updated_at &&
    (left.pending_elicitations_count ?? 0) === (right.pending_elicitations_count ?? 0) &&
    (left.git_branch ?? null) === (right.git_branch ?? null) &&
    (left.project_id ?? null) === (right.project_id ?? null) &&
    (left.workspace ?? null) === (right.workspace ?? null) &&
    (left.archived ?? false) === (right.archived ?? false) &&
    left.labels?.omni_project === right.labels?.omni_project
  );
}

/** Newest copy of every row in the sidebar's list cache, by id. */
function liveRows(queryClient: QueryClient): Map<string, Conversation> {
  const live = new Map<string, Conversation>();
  const entries = queryClient.getQueriesData<InfiniteData<ConversationsPage, string | undefined>>({
    queryKey: ["conversations"],
  });
  for (const [, data] of entries) {
    for (const page of data?.pages ?? []) {
      for (const row of page.data) {
        const known = live.get(row.id);
        if (!known || row.updated_at >= known.updated_at) live.set(row.id, row);
      }
    }
  }
  return live;
}

/**
 * Overlay the sidebar's live rows onto the canvas rows: a cached row that is
 * at least as recent and renders differently replaces the canvas copy. Rows
 * that became archived drop off. Returns the input when nothing changed.
 */
export function applyLiveRows(
  sessions: readonly Conversation[],
  live: ReadonlyMap<string, Conversation>,
): Conversation[] {
  let changed = false;
  const next: Conversation[] = [];
  for (const row of sessions) {
    const fresh = live.get(row.id);
    if (!fresh || fresh.updated_at < row.updated_at || sameCard(fresh, row)) {
      next.push(row);
      continue;
    }
    changed = true;
    if (isTopLevelActive(fresh)) next.push(fresh);
  }
  return changed ? next : [...sessions];
}

export function useCanvasSessions(): CanvasSessions {
  const queryClient = useQueryClient();
  const [awaitingStoredIdentity, setAwaitingStoredIdentity] = useState(() => {
    const viewerId = getCurrentUserId();
    if (rememberedSessions(queryClient, viewerId) !== undefined) return false;
    return viewerId === null && hasStoredSessionsForServer();
  });
  const [state, setState] = useState(() => {
    const remembered = awaitingStoredIdentity
      ? undefined
      : rememberedSessions(queryClient, getCurrentUserId());
    const sessions =
      remembered ?? (awaitingStoredIdentity ? [] : cachedSessionPreview(queryClient));
    return {
      sessions,
      loaded: remembered !== undefined || sessions.length > 0,
      loadingMore: false,
      complete: remembered !== undefined,
      networkConfirmed: false,
      error: null as string | null,
    };
  });
  const sessionsRef = useRef(state.sessions);
  const completeRef = useRef(state.complete);
  const inFlightRef = useRef<Promise<void> | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  // A persisted cache may be keyed by an identity that is still resolving on
  // startup. Wait rather than flashing the short anonymous sidebar preview.
  useEffect(() => {
    if (!awaitingStoredIdentity) return;
    let cancelled = false;
    void resolveIdentity().then((viewerId) => {
      if (cancelled) return;
      const remembered = rememberedSessions(queryClient, viewerId);
      const sessions = remembered ?? cachedSessionPreview(queryClient);
      sessionsRef.current = sessions;
      completeRef.current = remembered !== undefined;
      setState((current) => ({
        ...current,
        sessions,
        loaded: remembered !== undefined || sessions.length > 0,
        complete: remembered !== undefined,
      }));
      setAwaitingStoredIdentity(false);
    });
    return () => {
      cancelled = true;
    };
  }, [awaitingStoredIdentity, queryClient]);

  const refresh = useCallback((): Promise<void> => {
    if (inFlightRef.current) return inFlightRef.current;
    const existing = sessionsRef.current;
    // Once the full list is known, routine refreshes stay quiet.
    if (!completeRef.current) {
      setState((current) => ({ ...current, loadingMore: true }));
    }
    const request = (async () => {
      try {
        const serverId = currentServerId();
        const viewerId = await resolveIdentity();
        await loadAllSessions(existing, (progress) => {
          const sessions = progress.hasMore
            ? mergePartial(existing, progress.sessions)
            : progress.sessions;
          // Keep the completed list even if this page unmounted while loading.
          // A later Canvas visit can then paint the whole list immediately.
          if (!progress.hasMore && viewerId !== null) {
            rememberCompleteSessions(queryClient, serverId, viewerId, sessions);
          }
          if (!aliveRef.current) return;
          sessionsRef.current = sessions;
          if (!progress.hasMore) {
            completeRef.current = true;
          }
          setState((current) => ({
            ...current,
            sessions,
            loaded: true,
            complete: current.complete || !progress.hasMore,
            networkConfirmed: current.networkConfirmed || !progress.hasMore,
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
  }, [queryClient]);

  // Initial load, then poll like the sidebar does and catch up when the tab
  // becomes visible or the window regains focus.
  useEffect(() => {
    if (awaitingStoredIdentity) return;
    void refresh();
    const refreshIfVisible = () => {
      if (!document.hidden) void refresh();
    };
    const timer = setInterval(refreshIfVisible, SESSION_POLL_INTERVAL_MS);
    window.addEventListener("focus", refreshIfVisible);
    document.addEventListener("visibilitychange", refreshIfVisible);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", refreshIfVisible);
      document.removeEventListener("visibilitychange", refreshIfVisible);
    };
  }, [awaitingStoredIdentity, refresh]);

  // Live updates: the sessions stream patches the sidebar's cache in place;
  // mirror those rows so cards change with the sidebar instead of on the next poll.
  useEffect(() => {
    const mirror = () => {
      const next = applyLiveRows(sessionsRef.current, liveRows(queryClient));
      if (next === sessionsRef.current) return;
      if (
        next.length === sessionsRef.current.length &&
        next.every((row, i) => row === sessionsRef.current[i])
      ) {
        return;
      }
      sessionsRef.current = next;
      const viewerId = getCurrentUserId();
      if (completeRef.current && viewerId !== null) {
        const serverId = currentServerId();
        const previous = completeSessionsByClient.get(queryClient);
        completeSessionsByClient.set(queryClient, {
          serverId,
          viewerId,
          sessions: next,
          storedSignature:
            previous?.serverId === serverId && previous.viewerId === viewerId
              ? previous.storedSignature
              : null,
        });
      }
      setState((current) => ({ ...current, sessions: next }));
    };
    const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
      if (event.query.queryKey[0] === "conversations") mirror();
    });
    mirror();
    return unsubscribe;
  }, [queryClient]);

  return { ...state, refresh };
}
