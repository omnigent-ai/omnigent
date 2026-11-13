import { useQuery, type QueryClient } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { formatSessionCostUsd } from "@/lib/formatCost";
import { authenticatedFetch } from "@/lib/identity";
import { isTempConvId } from "@/lib/tempConversationId";
import { useChildSessions } from "./useChildSessions";

/** Display cost of one agent: its total including sub-agents, or a parent estimate. */
export interface AgentCost {
  costUsd: number | null;
  /** A parent-reported transcript estimate that the parent's total already includes. */
  estimated: boolean;
}

interface AgentCostPageWire {
  data: { id: string; cost_usd?: number | null; estimated?: boolean }[];
  last_id?: string | null;
  has_more?: boolean;
}

const AGENT_COSTS_QUERY_PREFIX = ["agent_costs"] as const;
const AGENT_COSTS_POLL_MS = 15_000;
const AGENT_COSTS_PAGE_SIZE = "1000";

function isOmnigentError(body: unknown): boolean {
  return typeof body === "object" && body !== null && "error" in body;
}

async function fetchAgentCostPages(
  sessionId: string,
  after: string | null,
  costs: Map<string, AgentCost>,
): Promise<Map<string, AgentCost> | null> {
  const params = new URLSearchParams({ limit: AGENT_COSTS_PAGE_SIZE });
  if (after !== null) params.set("after", after);
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/agent_costs?${params.toString()}`,
  );
  // A missing route (older or rolled-back server) answers 404 without the Omnigent
  // error envelope that a missing or hidden session carries; only it is unsupported.
  if (res.status === 404 && !isOmnigentError(await res.json().catch(() => null))) return null;
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const page = (await res.json()) as AgentCostPageWire;
  for (const row of page.data) {
    costs.set(row.id, { costUsd: row.cost_usd ?? null, estimated: row.estimated === true });
  }
  return page.has_more && page.last_id
    ? fetchAgentCostPages(sessionId, page.last_id, costs)
    : costs;
}

/**
 * Fetch costs for a session and every sub-agent beneath it, keyed by session id.
 * Resolves `null` when the server does not serve the breakdown.
 */
export function fetchAgentCosts(sessionId: string): Promise<Map<string, AgentCost> | null> {
  return fetchAgentCostPages(sessionId, null, new Map());
}

const AGENT_COSTS_REFRESH_MS = 2_000;
const pendingRefreshes = new WeakSet<QueryClient>();

/**
 * Refetch mounted breakdowns after a cost moved, skipping unsupported servers.
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
 * shown session total changes.
 */
export function useAgentCosts(sessionId: string | null) {
  const enabled = isFeatureEnabled(useServerInfo(), "session_overview");
  const id = enabled && sessionId !== null && !isTempConvId(sessionId) ? sessionId : null;
  const { children } = useChildSessions(id);
  const working = children.some((child) => child.busy);
  return useQuery({
    queryKey: [...AGENT_COSTS_QUERY_PREFIX, id],
    queryFn: () => fetchAgentCosts(id as string),
    enabled: id !== null,
    retry: false,
    refetchInterval: (query) =>
      working && query.state.data !== null ? AGENT_COSTS_POLL_MS : false,
  });
}

/** Short cost label, e.g. `"$0.42"`, `"~$0.10"` for an estimate, `null` when unknown. */
export function agentCostLabel(cost: AgentCost | undefined): string | null {
  if (typeof cost?.costUsd !== "number") return null;
  return `${cost.estimated ? "~" : ""}${formatSessionCostUsd(cost.costUsd)}`;
}

/** Tooltip naming what a cost label covers. */
export function agentCostTitle(cost: AgentCost | undefined): string {
  return cost?.estimated
    ? "Estimated from the sub-agent transcript; already in the parent's total"
    : "Includes this agent's own sub-agents";
}
