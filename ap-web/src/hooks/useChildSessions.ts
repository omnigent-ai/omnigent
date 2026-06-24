import { useQuery, useQueries, type QueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

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
}

/**
 * Wire shape of a single entry in the ``child_sessions`` response.
 * Field set matches the server's ``ChildSessionSummary`` Pydantic
 * model. Extra wire fields are silently ignored.
 */
interface ChildSessionWire {
  id: string;
  title: string | null;
  tool: string | null;
  session_name: string | null;
  labels?: Record<string, string>;
  current_task_status: string | null;
  last_task_error?: ChildSessionError | null;
  busy: boolean;
  last_message_preview?: string | null;
  pending_elicitations_count?: number;
}

interface ChildSessionsResponse {
  object: "list";
  data: ChildSessionWire[];
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
  let frontier = [rootId];
  for (let depth = 0; depth < maxDepth && frontier.length > 0; depth++) {
    const next: string[] = [];
    for (const id of frontier) {
      const children = queryClient.getQueryData<ChildSessionInfo[]>(childSessionsQueryKey(id));
      if (!children) continue;
      for (const child of children) {
        if (child.id === targetId) return true;
        next.push(child.id);
      }
    }
    frontier = next;
  }
  return false;
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
  return json.data.map((row) => ({
    id: row.id,
    title: row.title,
    tool: row.tool,
    session_name: row.session_name,
    labels: row.labels ?? {},
    current_task_status: row.current_task_status,
    last_task_error: parseChildSessionError(row.last_task_error),
    busy: row.busy,
    last_message_preview: row.last_message_preview ?? null,
    pending_elicitations_count: row.pending_elicitations_count ?? 0,
  }));
}

function childSessionsQueryOptions(sessionId: string, pollMs?: number | null) {
  return {
    queryKey: childSessionsQueryKey(sessionId),
    queryFn: () => fetchChildSessions(sessionId),
    staleTime: 60_000,
    retry: false as const,
    refetchOnMount: false as const,
    refetchInterval: pollMs ?? (false as const),
  };
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
 * :param includeDescendants: Walk the bounded tree for badge totals.
 */
export function useChildSessions(
  conversationId: string | null,
  pollMs?: number | null,
  includeDescendants = false,
): UseChildSessionsResult {
  const { data, isLoading, error } = useQuery({
    queryKey:
      conversationId === null
        ? ["conversation", null, "child_sessions"]
        : childSessionsQueryKey(conversationId),
    queryFn: () => fetchChildSessions(conversationId as string),
    enabled: conversationId !== null,
    staleTime: 60_000,
    retry: false,
    refetchOnMount: false,
    refetchInterval: pollMs ?? false,
  });
  const children = data ?? [];
  const grandchildQueries = useQueries({
    queries: includeDescendants
      ? children.map((child) => childSessionsQueryOptions(child.id, pollMs))
      : [],
  });
  const grandchildren = grandchildQueries.flatMap((query) => query.data ?? []);
  const greatGrandchildQueries = useQueries({
    queries: includeDescendants
      ? grandchildren.map((child) => childSessionsQueryOptions(child.id, pollMs))
      : [],
  });
  const greatGrandchildren = greatGrandchildQueries.flatMap((query) => query.data ?? []);

  if (!includeDescendants) {
    return {
      children,
      isLoading,
      error: (error as Error | null) ?? null,
    };
  }
  const seen = new Set<string | null>([conversationId]);
  const descendants = [children, grandchildren, greatGrandchildren]
    .flat()
    .filter((child) => {
      if (seen.has(child.id)) return false;
      seen.add(child.id);
      return true;
    });

  return {
    children: descendants,
    isLoading,
    error: (error as Error | null) ?? null,
  };
}
