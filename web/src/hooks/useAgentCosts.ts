import { useQuery, type QueryClient } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { formatSessionCostUsd } from "@/lib/formatCost";
import { authenticatedFetch } from "@/lib/identity";
import { isTempConvId } from "@/lib/tempConversationId";
import { isStaleCursorResponse, useChildSessions } from "./useChildSessions";

interface AgentCostPageWire {
  data: { id: string; cost_usd?: number | null; estimated_cost_usd?: number | null }[];
  last_id?: string | null;
  has_more?: boolean;
}

/** Display cost of one agent: its total including sub-agents, or a parent estimate. */
export interface AgentCost {
  costUsd: number | null;
  /** A parent-reported transcript estimate that the parent's total already includes. */
  estimated: boolean;
}

/** Agent costs keyed by session id. */
export type AgentCosts = Map<string, AgentCost>;

const AGENT_COSTS_QUERY_PREFIX = ["agent_costs"] as const;
const AGENT_COSTS_POLL_MS = 15_000;
const AGENT_COSTS_PAGE_SIZE = "1000";
const AGENT_COSTS_REFRESH_MS = 2_000;

async function fetchAgentCostPages(
  sessionId: string,
  after: string | null,
  costs: AgentCosts,
  restarted: boolean,
): Promise<AgentCosts | null> {
  const params = new URLSearchParams({ limit: AGENT_COSTS_PAGE_SIZE });
  if (after !== null) params.set("after", after);
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/agent_costs?${params.toString()}`,
  );
  // Servers without the route answer with the same 404 envelope as a missing session.
  if (res.status === 404 && after === null) return null;
  // The tree changed between pages: walk it again once.
  if (after !== null && !restarted && (await isStaleCursorResponse(res))) {
    return fetchAgentCostPages(sessionId, null, new Map(), true);
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const page = (await res.json()) as AgentCostPageWire;
  for (const row of page.data) {
    const estimated = row.cost_usd == null && typeof row.estimated_cost_usd === "number";
    costs.set(row.id, {
      costUsd: estimated ? (row.estimated_cost_usd ?? null) : (row.cost_usd ?? null),
      estimated,
    });
  }
  return page.has_more && page.last_id
    ? fetchAgentCostPages(sessionId, page.last_id, costs, restarted)
    : costs;
}

/**
 * Fetch costs for a session and every sub-agent beneath it.
 * Resolves `null` when the server gives no breakdown (any 404).
 */
export function fetchAgentCosts(sessionId: string): Promise<AgentCosts | null> {
  return fetchAgentCostPages(sessionId, null, new Map(), false);
}

const pendingRefreshes = new WeakSet<QueryClient>();

/**
 * Refetch mounted breakdowns after a cost moved, skipping servers without one.
 * Calls within {@link AGENT_COSTS_REFRESH_MS} coalesce into one trailing refetch.
 */
export function invalidateAgentCosts(queryClient: QueryClient): void {
  if (pendingRefreshes.has(queryClient)) return;
  pendingRefreshes.add(queryClient);
  setTimeout(() => {
    pendingRefreshes.delete(queryClient);
    void queryClient.invalidateQueries({
      queryKey: AGENT_COSTS_QUERY_PREFIX,
      predicate: (query) => query.state.data !== null,
    });
  }, AGENT_COSTS_REFRESH_MS);
}

/**
 * Agent costs for `sessionId`'s tree when the session overview feature is on.
 * Polls while a direct child works; the chat store invalidates it whenever the
 * shown session total changes. A `null` answer is final.
 */
export function useAgentCosts(sessionId: string | null) {
  const enabled = isFeatureEnabled(useServerInfo(), "session_overview");
  const id = enabled && sessionId !== null && !isTempConvId(sessionId) ? sessionId : null;
  // Cached child lists stay live: SessionUpdatesProvider watches each listed child.
  const { children } = useChildSessions(id);
  const working = children.some((child) => child.busy);
  return useQuery({
    queryKey: [...AGENT_COSTS_QUERY_PREFIX, id],
    queryFn: () => fetchAgentCosts(id as string),
    enabled: id !== null,
    retry: false,
    staleTime: (query) => (query.state.data === null ? Infinity : 0),
    refetchInterval: (query) =>
      working && query.state.data !== null ? AGENT_COSTS_POLL_MS : false,
  });
}

/** Short cost label, e.g. `"$0.42"`, `"~$0.10"` for an estimate, `null` when unknown. */
export function agentCostLabel(cost: AgentCost | undefined): string | null {
  if (typeof cost?.costUsd !== "number") return null;
  return `${cost.estimated ? "~" : ""}${formatSessionCostUsd(cost.costUsd)}`;
}
