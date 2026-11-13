import { useQuery } from "@tanstack/react-query";
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

interface AgentCostWire {
  id: string;
  cost_usd?: number | null;
  estimated?: boolean;
}

/** Prefix shared by every agent-cost query; the chat store invalidates it on cost changes. */
export const AGENT_COSTS_QUERY_PREFIX = ["agent_costs"] as const;

const AGENT_COSTS_POLL_MS = 15_000;

/** Fetch costs for a session and every sub-agent beneath it, keyed by session id. */
export async function fetchAgentCosts(sessionId: string): Promise<Map<string, AgentCost>> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/agent_costs`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as { data: AgentCostWire[] };
  return new Map(
    json.data.map((row) => [
      row.id,
      { costUsd: row.cost_usd ?? null, estimated: row.estimated === true },
    ]),
  );
}

/**
 * Agent costs for `sessionId`'s tree when the session overview feature is on.
 * Polls while a direct child works; cost changes on the bound session also
 * invalidate it.
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
    refetchInterval: working ? AGENT_COSTS_POLL_MS : false,
  });
}

/** Short cost label, e.g. `"$0.42"`, `"~$0.10"` for an estimate, `null` when unknown. */
export function agentCostLabel(cost: AgentCost | undefined): string | null {
  if (cost?.costUsd == null) return null;
  return `${cost.estimated ? "~" : ""}${formatSessionCostUsd(cost.costUsd)}`;
}

/** Tooltip naming what a cost label covers. */
export function agentCostTitle(cost: AgentCost | undefined): string {
  return cost?.estimated
    ? "Estimated from the sub-agent transcript; already in the parent's total"
    : "Includes this agent's own sub-agents";
}
