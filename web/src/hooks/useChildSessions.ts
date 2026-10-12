import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useSyncExternalStore } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { setSessionParent } from "@/lib/sessionHost";
import { isTempConvId } from "@/lib/tempConversationId";

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
}

interface ChildSessionsResponse {
  object: "list";
  data: ChildSessionWire[];
}

/**
 * Wire shape of one ``GET /v1/sessions/{id}/descendants`` row: a child
 * summary that also names its own parent and its depth below the root.
 */
interface DescendantSessionWire extends ChildSessionWire {
  parent_session_id: string;
  depth: number;
  created_at?: number;
  archived?: boolean;
}

interface DescendantsResponse {
  object: "list";
  data: DescendantSessionWire[];
  last_id: string | null;
  has_more: boolean;
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

/** TanStack Query key for a root session's whole sub-agent subtree. */
export function subagentTreeQueryKey(rootId: string): readonly unknown[] {
  return ["conversation", rootId, "descendants"];
}

/** True for any key built by ``subagentTreeQueryKey``. */
function isSubagentTreeQueryKey(key: readonly unknown[]): boolean {
  return key.length === 3 && key[0] === "conversation" && key[2] === "descendants";
}

/**
 * Cached result of one subtree fetch: every session the server listed under
 * the root, hidden archived rows included, plus the root itself. A child list
 * is "covered" by the tree while its parent is here, so refreshes of that
 * list reload the tree instead of fetching the node.
 */
interface SubagentTreeData {
  members: string[];
}

/**
 * Counter of refreshes requested while the root's subtree fetch was in
 * flight; the fetch re-reads the tree when it changed under it.
 */
function subagentTreeDirtyKey(rootId: string): readonly unknown[] {
  return ["conversation", rootId, "descendants", "dirty"];
}

/** Thrown by ``fetchSubagentTree`` so callers can tell a missing route apart. */
export class SubagentTreeFetchError extends Error {
  readonly status: number;

  constructor(status: number, statusText: string) {
    super(`${status} ${statusText}`);
    this.name = "SubagentTreeFetchError";
    this.status = status;
  }
}

/** Only a 404 means the server predates the subtree route. */
function isMissingSubtreeRoute(error: unknown): boolean {
  return error instanceof SubagentTreeFetchError && error.status === 404;
}

/**
 * True beneath a ``useSubagentTree`` that owns the per-parent caches (any
 * state but a server without the subtree route). ``useChildSessions`` then
 * reads its list from cache instead of fetching, so a rendered tree never
 * issues one request per node, not even while the subtree is still loading.
 */
export const SubagentTreeContext = createContext(false);

/**
 * Refresh a parent's child list. When a loaded subtree covers the parent the
 * tree is reloaded instead (one request re-seeds every list, including this
 * one); otherwise the list's own query refetches as before. A tree whose
 * last load failed for its root is retried the same way, and a tree still
 * loading is marked so it re-reads itself once the current fetch lands.
 *
 * @param queryClient - The app QueryClient holding child-session caches.
 * @param parentId - Parent whose child list changed, e.g. ``"conv_root"``.
 */
export function invalidateChildSessions(queryClient: QueryClient, parentId: string): Promise<void> {
  const trees = queryClient.getQueryCache().findAll({
    queryKey: ["conversation"],
    predicate: (query) => isSubagentTreeQueryKey(query.queryKey),
  });
  // Membership survives a failed refresh (the last good data is kept), so a
  // tree keeps owning its rows through a transient error; only a server
  // without the route hands them back to per-node fetching.
  const covering = trees.filter(
    (tree) =>
      !(tree.state.status === "error" && isMissingSubtreeRoute(tree.state.error)) &&
      ((tree.state.data as SubagentTreeData | undefined)?.members.includes(parentId) ?? false),
  );
  const failedRoots = trees.filter(
    (tree) =>
      tree.queryKey[1] === parentId &&
      tree.state.status === "error" &&
      !isMissingSubtreeRoute(tree.state.error),
  );
  // A fetch in flight read the server before this change; without a loaded
  // membership the event may concern any tree, so every loading one re-reads.
  for (const tree of trees) {
    if (tree.state.fetchStatus !== "fetching") continue;
    const rootId = tree.queryKey[1] as string;
    queryClient.setQueryData<number>(subagentTreeDirtyKey(rootId), (count) => (count ?? 0) + 1);
  }
  // Only a tree someone is watching refetches now; an unmounted one is marked
  // stale so it reloads when its rail reopens, and the list's own query still
  // refreshes for the consumers that stay mounted (the shell's count badge).
  const refreshes = [...covering, ...failedRoots].map((tree) =>
    queryClient.invalidateQueries({ queryKey: tree.queryKey }, { cancelRefetch: false }),
  );
  if (!covering.some((tree) => tree.getObserversCount() > 0)) {
    refreshes.push(queryClient.invalidateQueries({ queryKey: childSessionsQueryKey(parentId) }));
  }
  return Promise.all(refreshes).then(() => undefined);
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

// One shared empty list, so a consumer comparing identities between renders
// (the graph view's collector map) sees no change while a list is unloaded.
const NO_CHILDREN: ChildSessionInfo[] = [];

/**
 * Fetch child sessions for a parent session.
 *
 * Exported for unit testing of the HTTP-shape contract; production
 * code should call ``useChildSessions``.
 */
export async function fetchChildSessions(sessionId: string): Promise<ChildSessionInfo[]> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/child_sessions`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as ChildSessionsResponse;
  // Children run on this session's runner: record the link so their
  // session-scoped requests key by this session's host before their own
  // snapshot loads.
  for (const row of json.data) setSessionParent(row.id, sessionId);
  return json.data.map(childInfoFromWire);
}

function childInfoFromWire(row: ChildSessionWire): ChildSessionInfo {
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

/**
 * Drop archived rows unless live work sits beneath them, so an archived
 * session stays in the tree only while it connects a live descendant.
 */
function withoutSettledArchived(rows: DescendantSessionWire[]): DescendantSessionWire[] {
  const live = new Set<string>();
  // Deepest rows first, so every child is seen before its parent. Archiving
  // does not end a session's work, so a busy or waiting archived row is live.
  for (const row of [...rows].sort((a, b) => b.depth - a.depth)) {
    const working = row.busy || (row.pending_elicitations_count ?? 0) > 0;
    if (!row.archived || working || live.has(row.id)) {
      live.add(row.id);
      live.add(row.parent_session_id);
    }
  }
  return rows.filter((row) => live.has(row.id));
}

/** Read every ``descendants`` page under ``rootId`` in the server's order. */
async function fetchDescendantRows(rootId: string): Promise<DescendantSessionWire[]> {
  const rows: DescendantSessionWire[] = [];
  const cursors = new Set<string>();
  let after: string | null = null;
  // Each page's cursor comes from the previous response, so the awaits
  // cannot be parallelized.
  /* oxlint-disable no-await-in-loop */
  do {
    const params = new URLSearchParams({ max_depth: String(MAX_TREE_DEPTH), limit: "1000" });
    if (after !== null) params.set("after", after);
    const res = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(rootId)}/descendants?${params.toString()}`,
    );
    if (!res.ok) throw new SubagentTreeFetchError(res.status, res.statusText);
    const json = (await res.json()) as DescendantsResponse;
    rows.push(...json.data);
    const next = json.has_more ? json.last_id : null;
    // A cursor seen before means the server is not advancing; stop here.
    if (next !== null && cursors.has(next)) throw new Error("descendants cursor did not advance");
    if (next !== null) cursors.add(next);
    after = next;
  } while (after !== null);
  /* oxlint-enable no-await-in-loop */
  return rows;
}

/**
 * Fetch every sub-agent under ``rootId`` down to ``MAX_TREE_DEPTH`` through
 * ``GET /v1/sessions/{id}/descendants`` — one request per page, not one per
 * node — and seed each parent's ``child_sessions`` cache from the result, so
 * the rows the Agents rail renders find their children already cached. A
 * refresh requested while the pages were loading re-reads the tree before
 * returning, so no event lands between the server's snapshot and the cache.
 *
 * Exported for unit testing; production code should call ``useSubagentTree``.
 *
 * @param rootId - Root session whose subtree to load, e.g. ``"conv_root"``.
 * @param queryClient - The app QueryClient whose child-session caches to seed.
 * @returns The sessions the tree covers (the root and every listed descendant).
 */
export async function fetchSubagentTree(
  rootId: string,
  queryClient: QueryClient,
): Promise<SubagentTreeData> {
  const dirtyKey = subagentTreeDirtyKey(rootId);
  let rows: DescendantSessionWire[] = [];
  let startedAt = Date.now();
  let dirty = true;
  // Bounded so a storm of events cannot keep this fetch running; a change
  // that lands during the last pass schedules one more refresh instead.
  /* oxlint-disable no-await-in-loop */
  for (let pass = 0; pass < 3 && dirty; pass++) {
    startedAt = Date.now();
    const dirtyAtStart = queryClient.getQueryData<number>(dirtyKey) ?? 0;
    rows = await fetchDescendantRows(rootId);
    dirty = (queryClient.getQueryData<number>(dirtyKey) ?? 0) !== dirtyAtStart;
  }
  /* oxlint-enable no-await-in-loop */
  if (dirty) {
    setTimeout(() => {
      void queryClient.invalidateQueries({ queryKey: subagentTreeQueryKey(rootId) });
    }, 0);
  }
  const byParent = new Map<string, ChildSessionInfo[]>([[rootId, []]]);
  for (const row of withoutSettledArchived(rows)) {
    setSessionParent(row.id, row.parent_session_id);
    const siblings = byParent.get(row.parent_session_id) ?? [];
    siblings.push(childInfoFromWire(row));
    byParent.set(row.parent_session_id, siblings);
    // Rows within the depth cap render a child list of their own; seed an
    // empty one so a childless row never asks the server.
    if (row.depth < MAX_TREE_DEPTH && !byParent.has(row.id)) byParent.set(row.id, []);
  }
  // Pages come in primary-key order; the rail shows siblings newest-first,
  // same-second siblings by descending id so the order is stable.
  const createdAt = new Map(rows.map((row) => [row.id, row.created_at ?? 0]));
  for (const [parentId, children] of byParent) {
    children.sort(
      (a, b) => (createdAt.get(b.id) ?? 0) - (createdAt.get(a.id) ?? 0) || b.id.localeCompare(a.id),
    );
    // A list patched by a live event while this fetch was in flight is newer
    // than the snapshot; leave it for the next refresh.
    const updatedAt = queryClient.getQueryState(childSessionsQueryKey(parentId))?.dataUpdatedAt;
    if (updatedAt !== undefined && updatedAt > startedAt) continue;
    queryClient.setQueryData(childSessionsQueryKey(parentId), children);
  }
  return { members: [rootId, ...rows.map((row) => row.id)] };
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
  // Under a subtree fetch the list comes from its cache; stay subscribed to
  // it without fetching, so invalidations never fan out one request per node.
  const fromTree = useContext(SubagentTreeContext);
  const { data, isLoading, error } = useQuery({
    queryKey:
      sessionId === null
        ? ["conversation", null, "child_sessions"]
        : childSessionsQueryKey(sessionId),
    queryFn: () => fetchChildSessions(sessionId as string),
    enabled: sessionId !== null && !fromTree,
    staleTime: 60_000,
    retry: false,
    refetchOnMount: false,
    refetchInterval: pollMs ?? false,
  });
  return {
    children: data ?? NO_CHILDREN,
    isLoading,
    error: (error as Error | null) ?? null,
  };
}

interface UseSubagentTreeResult extends UseChildSessionsResult {
  /**
   * True while the subtree fetch owns the per-parent caches, i.e. unless the
   * server has no subtree route. Provide it through ``SubagentTreeContext``.
   */
  fromTree: boolean;
}

/**
 * Child-session list for the root of the Agents rail, loaded with the whole
 * subtree in one request so the per-row ``useChildSessions`` calls beneath
 * it are served from cache instead of fetching once per node. Render those
 * rows inside ``SubagentTreeContext`` with ``fromTree`` so they never fetch
 * for themselves and refreshes (``invalidateChildSessions``) reload the tree
 * rather than each node. When the server has no subtree endpoint the hook
 * falls back to the per-node fetch; any other failure is surfaced as
 * ``error`` and retried on the next refresh.
 *
 * @param rootSessionId - Root session id, or ``null`` to disable the query.
 */
export function useSubagentTree(rootSessionId: string | null): UseSubagentTreeResult {
  const queryClient = useQueryClient();
  const sessionId = isTempConvId(rootSessionId) ? null : rootSessionId;
  const tree = useQuery({
    queryKey:
      sessionId === null ? ["conversation", null, "descendants"] : subagentTreeQueryKey(sessionId),
    queryFn: () => fetchSubagentTree(sessionId as string, queryClient),
    enabled: sessionId !== null,
    // A rail reopened after a refresh marked its unmounted tree stale must
    // reload it, so mounting refetches whenever the tree is stale.
    staleTime: 60_000,
    retry: false,
  });
  const legacy = tree.isError && isMissingSubtreeRoute(tree.error);
  const direct = useQuery({
    queryKey:
      sessionId === null
        ? ["conversation", null, "child_sessions"]
        : childSessionsQueryKey(sessionId),
    queryFn: () => fetchChildSessions(sessionId as string),
    enabled: sessionId !== null && legacy,
    staleTime: 60_000,
    retry: false,
    refetchOnMount: false,
  });
  return {
    children: direct.data ?? NO_CHILDREN,
    isLoading: (sessionId !== null && tree.isPending) || direct.isLoading,
    error: ((legacy ? direct.error : tree.error) as Error | null) ?? null,
    fromTree: sessionId !== null && !legacy,
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
