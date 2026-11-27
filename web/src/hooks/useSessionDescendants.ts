// A session's whole sub-agent tree, read level by level for the Pull Requests list's sub-agent rows.

import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import {
  type ChildSessionInfo,
  type ChildSessionsResponse,
  type ChildSessionWire,
  isStaleCursorResponse,
  MAX_TREE_DEPTH,
  parseChildSessionError,
} from "@/hooks/useChildSessions";
import { authenticatedFetch } from "@/lib/identity";
import { setSessionParent } from "@/lib/sessionHost";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  recordSubAgentReadFailure,
  classifySubAgentReadError,
} from "@/lib/subAgentPullRequestDiagnostics";

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
    const stale = await isStaleCursorResponse(res);
    throw Object.assign(new Error(`${res.status} ${res.statusText}`), {
      status: res.status,
      staleCursor: stale,
    });
  }
  return (await res.json()) as ChildSessionsResponse;
}

/** The server's 400 `stale_cursor`: the list changed under a page's cursor. */
const isStaleCursor = (error: unknown) =>
  (error as { staleCursor?: unknown } | null)?.staleCursor === true;
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
  const finish = (truncated: boolean): SessionDescendants => ({ rows, truncated });
  /* oxlint-disable no-await-in-loop */
  // Each level needs the one before it, so the levels run in sequence.
  for (let depth = 1; depth <= MAX_TREE_DEPTH && frontier.length > 0; depth++) {
    const budget = SUB_AGENT_LIST_CAP - rows.length;
    // A full list with more levels below may have more sub-agents; a check is conservative here.
    if (budget <= 0) return finish(true);
    const level = await readTreeLevel(frontier, budget, signal);
    // Registered before the next level reads, so its requests route through each real parent.
    for (const { parentId, child } of level.children) {
      rows.push({ info: childSessionInfoFromWire(child), parentId, depth });
      setSessionParent(child.id, parentId);
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
 * depths, which an older server or a tree too large for one read does, or with a deeper
 * row that doesn't name its parent. Each row's real parent is
 * registered with `setSessionParent`, so a grandchild routes through its own parent.
 *
 * Exported for unit testing; production code calls {@link useSessionDescendants}.
 */
export async function fetchSessionDescendants(
  rootId: string,
  signal: AbortSignal = new AbortController().signal,
): Promise<SessionDescendants> {
  try {
    return await listSessionDescendants(rootId, signal);
  } catch (error) {
    // The tree changed between pages: drop what was read and read it all again, once.
    if (!isStaleCursor(error) || signal.aborted) throw error;
    return listSessionDescendants(rootId, signal);
  }
}

/**
 * Whether a listed row says where it sits in the tree. An older server, or one whose tree was too
 * large for one read, lists direct children only, with a null or absent depth.
 */
const hasTreePosition = (row: ChildSessionWire): boolean =>
  typeof row.depth === "number" && (row.depth <= 1 || !!row.parent_session_id);

async function listSessionDescendants(
  rootId: string,
  signal: AbortSignal,
): Promise<SessionDescendants> {
  const rows: ChildSessionWire[] = [];
  let after: string | null = null;
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
    if (!page.data.every(hasTreePosition)) return walkSubAgentTree(rootId, signal);
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
