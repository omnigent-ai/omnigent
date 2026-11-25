import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { setSessionParent } from "@/lib/sessionHost";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  recordSubAgentReadFailure,
  classifySubAgentReadError,
} from "@/lib/subAgentPullRequestDiagnostics";

/**
 * Maximum depth of sub-agent nesting the Agents rail renders, counted
 * in levels below the root ("main") session: 1 = children,
 * 2 = grandchildren, 3 = great-grandchildren. Deeper descendants are
 * neither fetched nor rendered.
 */
export const MAX_TREE_DEPTH = 3;

export interface ChildSessionError {
  code: string;
  message: string;
}

/**
 * UI-facing child (sub-agent) session record.
 *
 * Mirrors the ``ChildSessionSummary`` schema returned by
 * ``GET /v1/sessions/{id}/child_sessions``. Only the fields the UI
 * renders or addresses are surfaced — extra wire fields are
 * tolerated and ignored.
 */
export interface ChildSessionInfo {
  /** Child conversation/session identifier, e.g. ``"conv_child123"``. */
  id: string;
  /** Full title, ``"{tool}:{session_name}"``, e.g. ``"researcher:auth"``. */
  title: string | null;
  /** Human-readable task-derived label, e.g. ``"Investigate auth flow"``. */
  task_summary: string | null;
  /** Sub-agent type prefix, e.g. ``"researcher"``. */
  tool: string | null;
  /** Sub-agent instance name suffix, e.g. ``"auth"``. */
  session_name: string | null;
  /** Session-scoped labels from the child conversation. */
  labels?: Record<string, string>;
  /** Status of the latest task, e.g. ``"completed"``. */
  current_task_status: string | null;
  /** Durable error details from the latest failed child run. */
  last_task_error?: ChildSessionError | null;
  /** True when the latest task is in an active (queued/in_progress) state. */
  busy: boolean;
  /**
   * Single-line preview of the most recent message in the child's
   * conversation, truncated to ~150 chars with a trailing ellipsis.
   * ``null`` when the child has no message items yet.
   */
  last_message_preview: string | null;
  /**
   * Number of approval / input prompts the child is currently blocked
   * on. ``> 0`` means the sub-agent is parked awaiting user input, and
   * the Agents rail renders an "awaiting input" badge for it.
   */
  pending_elicitations_count: number;
  /**
   * Model the intelligent router picked for this sub-agent, e.g.
   * ``"databricks-claude-sonnet-5"``. ``null``/absent when the child was
   * not routed (routing off, or a server that predates the field).
   */
  routed_model?: string | null;
}

/**
 * Wire shape of a single entry in the ``child_sessions`` response.
 * Field set matches the server's ``ChildSessionSummary`` Pydantic
 * model. Extra wire fields are silently ignored.
 */
interface ChildSessionWire {
  id: string;
  title: string | null;
  task_summary?: string | null;
  tool: string | null;
  session_name: string | null;
  labels?: Record<string, string>;
  current_task_status: string | null;
  last_task_error?: ChildSessionError | null;
  busy: boolean;
  last_message_preview?: string | null;
  pending_elicitations_count?: number;
  routed_model?: string | null;
  /** Set on rows of a ``descendants`` listing: the row's own parent. */
  parent_session_id?: string | null;
  /** Set on rows of a ``descendants`` listing: levels below the requested session. */
  depth?: number;
}

interface ChildSessionsResponse {
  object: "list";
  data: ChildSessionWire[];
  last_id?: string | null;
  has_more?: boolean;
}

const CHILD_SESSIONS_PAGE_SIZE = "100";

/** True for the server's 400 `stale_cursor`: the list changed under a page cursor. */
export async function isStaleCursorResponse(res: Response): Promise<boolean> {
  if (res.status !== 400) return false;
  const body = (await res
    .clone()
    .json()
    .catch(() => null)) as { error?: { code?: unknown } } | null;
  return body?.error?.code === "stale_cursor";
}

async function fetchChildSessionRows(
  sessionId: string,
  after: string | null,
  rows: ChildSessionWire[],
  restarted: boolean,
): Promise<ChildSessionWire[]> {
  // The first page keeps the plain URL; later pages continue after its last row.
  const query =
    after === null ? "" : `?${new URLSearchParams({ limit: CHILD_SESSIONS_PAGE_SIZE, after })}`;
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/child_sessions${query}`,
  );
  // The list changed between pages: read it again once.
  if (after !== null && !restarted && (await isStaleCursorResponse(res))) {
    return fetchChildSessionRows(sessionId, null, [], true);
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as ChildSessionsResponse;
  rows.push(...json.data);
  return json.has_more && json.last_id
    ? fetchChildSessionRows(sessionId, json.last_id, rows, restarted)
    : rows;
}

/**
 * TanStack Query key for a session's child sessions.
 *
 * Exported so the SSE handler can invalidate the same cache entry
 * on ``session.created`` events (live updates).
 */
export function childSessionsQueryKey(conversationId: string): readonly unknown[] {
  return ["conversation", conversationId, "child_sessions"];
}

/** True for any key built by ``childSessionsQueryKey``. */
function isChildSessionsQueryKey(key: readonly unknown[]): boolean {
  return key.length === 3 && key[0] === "conversation" && key[2] === "child_sessions";
}

/**
 * Walk the cached child-session lists to test whether ``targetId`` is
 * a known descendant of ``rootId``.
 *
 * Synchronous and cache-only — never fetches. Used by AppShell's
 * sticky root resolution: when the user clicks a row the rail just
 * rendered, every ancestor's child list is already cached, so
 * membership here means "the rail listed this session under that
 * root" and the root can be held steady while the target's snapshot
 * loads.
 *
 * @param queryClient - The app QueryClient holding child-session caches.
 * @param rootId - Root session whose cached tree to walk, e.g. ``"conv_root"``.
 * @param targetId - Session to look for, e.g. ``"conv_grandchild"``.
 * @param maxDepth - Levels below the root to examine, e.g. ``MAX_TREE_DEPTH``.
 * @returns True when ``targetId`` appears in the cached tree under ``rootId``.
 */
export function cachedTreeContains(
  queryClient: QueryClient,
  rootId: string,
  targetId: string,
  maxDepth: number,
): boolean {
  return findCachedDescendant(queryClient, rootId, targetId, maxDepth) !== null;
}

/**
 * Find ``targetId``'s summary in the cached child-session lists under
 * ``rootId``. Synchronous and cache-only, like ``cachedTreeContains``.
 *
 * @param queryClient - The app QueryClient holding child-session caches.
 * @param rootId - Root session whose cached tree to walk, e.g. ``"conv_root"``.
 * @param targetId - Session to look for, e.g. ``"conv_grandchild"``.
 * @param maxDepth - Levels below the root to examine, e.g. ``MAX_TREE_DEPTH``.
 * @returns The cached summary, or ``null`` when no cached list holds it.
 */
export function findCachedDescendant(
  queryClient: QueryClient,
  rootId: string,
  targetId: string,
  maxDepth: number,
): ChildSessionInfo | null {
  const visited = new Set([rootId]);
  let frontier = [rootId];
  for (let depth = 0; depth < maxDepth && frontier.length > 0; depth++) {
    const next: string[] = [];
    for (const id of frontier) {
      const children = queryClient.getQueryData<ChildSessionInfo[]>(childSessionsQueryKey(id));
      if (!children) continue;
      for (const child of children) {
        if (child.id === targetId) return child;
        if (visited.has(child.id)) continue;
        visited.add(child.id);
        next.push(child.id);
      }
    }
    frontier = next;
  }
  return null;
}

/**
 * Sentinel value used in place of a session id for the rail/panel's
 * "main" entry. The panel resolves it to the currently-viewed parent
 * conversation id at runtime so the same code path serves both main
 * and child sessions.
 */
export const MAIN_EXECUTION_LOG_KEY = "main";

/**
 * Stable tab id for an execution-log entry, used as the panel's
 * Tabs trigger value and as the message between the rail and the
 * panel. Format is ``executionLog:<id>``, where ``<id>`` is either
 * ``"main"`` (the parent session) or a child session id.
 */
export function executionLogTabKey(idOrMain: string): string {
  return `executionLog:${idOrMain}`;
}

function parseChildSessionError(value: unknown): ChildSessionError | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (typeof record.code !== "string" || typeof record.message !== "string") return null;
  if (!record.code || !record.message) return null;
  return { code: record.code, message: record.message };
}

interface UseChildSessionsResult {
  children: ChildSessionInfo[];
  isLoading: boolean;
  error: Error | null;
}

/**
 * Fetch every child session of a parent session, following the pages.
 *
 * Exported for unit testing of the HTTP-shape contract; production
 * code should call ``useChildSessions``.
 */
export async function fetchChildSessions(sessionId: string): Promise<ChildSessionInfo[]> {
  const rows = await fetchChildSessionRows(sessionId, null, [], false);
  // Children run on this session's runner: record the link so their
  // session-scoped requests key by this session's host before their own
  // snapshot loads.
  for (const row of rows) setSessionParent(row.id, sessionId);
  return rows.map((row) => ({
    id: row.id,
    title: row.title,
    task_summary: row.task_summary ?? null,
    tool: row.tool,
    session_name: row.session_name,
    labels: row.labels ?? {},
    current_task_status: row.current_task_status,
    last_task_error: parseChildSessionError(row.last_task_error),
    busy: row.busy,
    last_message_preview: row.last_message_preview ?? null,
    pending_elicitations_count: row.pending_elicitations_count ?? 0,
    routed_model: row.routed_model ?? null,
  }));
}

/**
 * Live child-session list for a conversation, served by
 * ``GET /v1/sessions/{id}/child_sessions``.
 *
 * The ``session.created`` handler in ``chatStore.ts`` invalidates
 * this query key on each spawn so newly-created child sessions
 * appear without waiting for the next poll or a manual refresh.
 *
 * :param conversationId: Parent session/conversation identifier,
 *     or ``null`` to disable the query.
 * :param pollMs: Optional poll interval in milliseconds. When set,
 *     the query refetches every ``pollMs`` ms (paused while the
 *     tab is backgrounded). The execution-logs panel passes a value
 *     here so the dropdown updates when new sub-agents spawn;
 *     callers that just need a snapshot (the rail card) can omit it.
 */
export function useChildSessions(
  conversationId: string | null,
  pollMs?: number | null,
): UseChildSessionsResult {
  const sessionId = isTempConvId(conversationId) ? null : conversationId;
  const { data, isLoading, error } = useQuery({
    queryKey:
      sessionId === null
        ? ["conversation", null, "child_sessions"]
        : childSessionsQueryKey(sessionId),
    queryFn: () => fetchChildSessions(sessionId as string),
    enabled: sessionId !== null,
    staleTime: 60_000,
    retry: false,
    refetchOnMount: false,
    refetchInterval: pollMs ?? false,
  });
  return {
    children: data ?? [],
    isLoading,
    error: (error as Error | null) ?? null,
  };
}

/**
 * Summary of ``targetId`` as listed under ``ancestorId``'s spawn tree.
 *
 * Direct children come from ``useChildSessions`` (fetched when not yet
 * cached); deeper descendants are read from lists the Agents rail has
 * cached. Re-renders when any cached child-session list changes.
 *
 * @param ancestorId - Session whose tree to search, e.g. ``"conv_root"``;
 *   ``null`` disables the lookup.
 * @param targetId - Descendant to find, e.g. ``"conv_child123"``.
 * @returns The descendant's summary, or ``null`` while unknown.
 */
export function useDescendantSession(
  ancestorId: string | null,
  targetId: string,
): ChildSessionInfo | null {
  const queryClient = useQueryClient();
  useChildSessions(ancestorId);
  const subscribe = useCallback(
    (onChange: () => void) =>
      queryClient.getQueryCache().subscribe((event) => {
        if (event.type === "updated" && isChildSessionsQueryKey(event.query.queryKey)) onChange();
      }),
    [queryClient],
  );
  // Cached rows keep their identity until their list changes, so the
  // snapshot is stable between unrelated cache events.
  return useSyncExternalStore(subscribe, () =>
    ancestorId === null
      ? null
      : findCachedDescendant(queryClient, ancestorId, targetId, MAX_TREE_DEPTH),
  );
}

function childSessionInfoFromWire(row: ChildSessionWire): ChildSessionInfo {
  return {
    id: row.id,
    title: row.title,
    task_summary: row.task_summary ?? null,
    tool: row.tool,
    session_name: row.session_name,
    labels: row.labels ?? {},
    current_task_status: row.current_task_status,
    last_task_error: parseChildSessionError(row.last_task_error),
    busy: row.busy,
    last_message_preview: row.last_message_preview ?? null,
    pending_elicitations_count: row.pending_elicitations_count ?? 0,
    routed_model: row.routed_model ?? null,
  };
}
/** Sub-agents a session's pull request list reads at once, at most. */
export const SUB_AGENT_LIST_CAP = 1000;
/** One sub-agent in a session's tree, with the session that spawned it. */
export interface SessionDescendant {
  info: ChildSessionInfo;
  /** The sub-agent's own parent, so a grandchild routes by its parent, not the root. */
  parentId: string;
  /** Levels below the requested session. */
  depth: number;
}
export interface SessionDescendants {
  /** Breadth-first: each level's rows in the order the server lists them. */
  rows: SessionDescendant[];
  /** The tree had more sub-agents than {@link SUB_AGENT_LIST_CAP}; the first ones are in `rows`. */
  truncated: boolean;
}
/**
 * TanStack Query key for every sub-agent under a session, down to {@link MAX_TREE_DEPTH}.
 *
 * Separate from {@link childSessionsQueryKey}, which stays the direct children the Agents rail
 * shows. Invalidate it with {@link invalidateSessionDescendants}.
 */
export function sessionDescendantsQueryKey(rootId: string): readonly unknown[] {
  return ["conversation", rootId, "descendants"];
}
/** Refetches every cached sub-agent tree, after a spawn or a change to a sub-agent's state. */
export function invalidateSessionDescendants(queryClient: QueryClient): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: ["conversation"],
    predicate: (query) => isSessionDescendantsQueryKey(query.queryKey),
  });
}
function isSessionDescendantsQueryKey(key: readonly unknown[]): boolean {
  return key.length === 3 && key[0] === "conversation" && key[2] === "descendants";
}
async function fetchChildPage(
  sessionId: string,
  query: URLSearchParams,
  signal?: AbortSignal,
): Promise<ChildSessionsResponse> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/child_sessions?${query}`,
    signal ? { signal } : undefined,
  );
  if (!res.ok) {
    throw Object.assign(new Error(`${res.status} ${res.statusText}`), { status: res.status });
  }
  return (await res.json()) as ChildSessionsResponse;
}
/** How many parents of one level are read at once: the one being placed and the next three. */
const LEVEL_READ_AHEAD = 4;
/** A direct child read on one level of the walk, with the parent whose listing returned it. */
interface LevelChild {
  parentId: string;
  child: ChildSessionWire;
}
/**
 * One level's direct children in breadth-first order, up to `budget` rows, and whether any may
 * remain unread. The parent being placed pages on until the budget is met, while only the next
 * parents' first pages are read ahead, so a slow early parent can't let its siblings read the cap.
 */
async function readTreeLevel(
  frontier: readonly string[],
  budget: number,
  signal: AbortSignal,
): Promise<{ children: LevelChild[]; truncated: boolean }> {
  // Reads ahead that the level never reaches stop once it is placed, or with the caller's abort.
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) controller.abort();
  const firstPages: Promise<ChildSessionsResponse>[] = [];
  const firstPage = (index: number) => {
    if (!firstPages[index]) {
      const query = new URLSearchParams({ limit: "200" });
      firstPages[index] = fetchChildPage(frontier[index]!, query, controller.signal);
      // An aborted read ahead is expected; a read the level does reach still throws when awaited.
      firstPages[index].catch(() => {});
    }
    return firstPages[index];
  };
  const children: LevelChild[] = [];
  /* oxlint-disable no-await-in-loop */
  // Parents are placed in order, so each waits for the one before it.
  try {
    for (let index = 0; index < frontier.length; index++) {
      // Parents left unread once the budget is full may have children, so this is conservative.
      if (children.length >= budget) return { children, truncated: true };
      const lastAhead = Math.min(frontier.length, index + LEVEL_READ_AHEAD);
      for (let ahead = index; ahead < lastAhead; ahead++) firstPage(ahead);
      const parentId = frontier[index]!;
      let page = await firstPage(index);
      for (;;) {
        for (const child of page.data) {
          if (children.length >= budget) return { children, truncated: true };
          children.push({ parentId, child });
        }
        if (page.has_more !== true || !page.last_id) break;
        if (children.length >= budget) return { children, truncated: true };
        const query = new URLSearchParams({ limit: "200", after: page.last_id });
        page = await fetchChildPage(parentId, query, controller.signal);
      }
    }
    return { children, truncated: false };
  } finally {
    signal.removeEventListener("abort", abort);
    controller.abort();
  }
  /* oxlint-enable no-await-in-loop */
}
/**
 * Walks a tree one level at a time, for a server whose ``descendants`` listing is absent or
 * whose tree is too large for one read. Each level stops reading once its rows, in breadth-first
 * order, fill the cap, so the cap bounds the requests as well as the rows.
 */
async function walkSubAgentTree(rootId: string, signal: AbortSignal): Promise<SessionDescendants> {
  const rows: SessionDescendant[] = [];
  let frontier = [rootId];
  // Every return registers the real parent of each row it keeps, truncated or not.
  const finish = (truncated: boolean): SessionDescendants => {
    for (const row of rows) setSessionParent(row.info.id, row.parentId);
    return { rows, truncated };
  };
  /* oxlint-disable no-await-in-loop */
  // Each level needs the one before it, so the levels run in sequence.
  for (let depth = 1; depth <= MAX_TREE_DEPTH && frontier.length > 0; depth++) {
    const budget = SUB_AGENT_LIST_CAP - rows.length;
    // A full list with more levels below may have more sub-agents; a check is conservative here.
    if (budget <= 0) return finish(true);
    const level = await readTreeLevel(frontier, budget, signal);
    for (const { parentId, child } of level.children) {
      rows.push({ info: childSessionInfoFromWire(child), parentId, depth });
    }
    if (level.truncated) return finish(true);
    frontier = level.children.map(({ child }) => child.id);
  }
  /* oxlint-enable no-await-in-loop */
  return finish(false);
}
/**
 * Every sub-agent under a session, down to {@link MAX_TREE_DEPTH}, in one listing. Pages until
 * the last page or the cap. Falls back to a level-by-level walk when the server answers without
 * depths, which an older server or a tree too large for one read does. Each row's real parent is
 * registered with `setSessionParent`, so a grandchild routes through its own parent.
 *
 * Exported for unit testing; production code calls {@link useSessionDescendants}.
 */
export async function fetchSessionDescendants(
  rootId: string,
  signal: AbortSignal = new AbortController().signal,
): Promise<SessionDescendants> {
  const rows: ChildSessionWire[] = [];
  let after: string | null = null;
  let withDepth: boolean | undefined;
  let hasMore: boolean;
  /* oxlint-disable no-await-in-loop */
  do {
    const query = new URLSearchParams({
      descendants: "true",
      max_depth: String(MAX_TREE_DEPTH),
      limit: "200",
      ...(after ? { after } : {}),
    });
    const page = await fetchChildPage(rootId, query, signal);
    withDepth ??= page.data.length === 0 ? true : page.data[0]!.depth !== undefined;
    if (!withDepth) return walkSubAgentTree(rootId, signal);
    rows.push(...page.data);
    hasMore = page.has_more === true;
    after = hasMore && page.last_id ? page.last_id : null;
  } while (after && rows.length < SUB_AGENT_LIST_CAP);
  /* oxlint-enable no-await-in-loop */
  const kept = rows.slice(0, SUB_AGENT_LIST_CAP).map((row) => ({
    info: childSessionInfoFromWire(row),
    parentId: row.parent_session_id ?? rootId,
    depth: row.depth ?? 1,
  }));
  for (const row of kept) setSessionParent(row.info.id, row.parentId);
  const truncated =
    rows.length > SUB_AGENT_LIST_CAP || (hasMore && rows.length >= SUB_AGENT_LIST_CAP);
  return { rows: kept, truncated };
}
/**
 * Every sub-agent under a session, down to {@link MAX_TREE_DEPTH}, for its pull request list.
 *
 * @param rootId - The viewed session, e.g. `"conv_root"`; `null` disables the query.
 * @param options.enabled - False skips the request, such as while the list is off screen.
 * @param options.pollMs - Refetch interval in milliseconds, paused while the tab is hidden.
 */
export function useSessionDescendants(
  rootId: string | null,
  {
    enabled = true,
    pollMs,
    origin = "list",
  }: { enabled?: boolean; pollMs?: number | null; origin?: "list" | "open" | "composer" } = {},
) {
  const sessionId = isTempConvId(rootId) ? null : rootId;
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: sessionDescendantsQueryKey(sessionId ?? ""),
    queryFn: async ({ signal }) => {
      const key = sessionDescendantsQueryKey(sessionId as string);
      const trigger =
        origin !== "list"
          ? origin
          : queryClient.getQueryData(key) === undefined
            ? "initial"
            : "refresh";
      try {
        return await fetchSessionDescendants(sessionId as string, signal);
      } catch (error) {
        if (!signal.aborted) {
          recordSubAgentReadFailure({
            stage: "descendants",
            trigger,
            parentSessionId: sessionId as string,
            ...classifySubAgentReadError(error),
          });
        }
        throw error;
      }
    },
    enabled: sessionId !== null && enabled,
    staleTime: 30_000,
    retry: false,
    refetchInterval: pollMs ?? false,
  });
}
