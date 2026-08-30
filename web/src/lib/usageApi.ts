import { authenticatedFetch } from "./identity";

// ── Wire types (snake_case from server) ─────────────────────────

interface DailyCostWire {
  day: string;
  cost_usd: number;
}

interface SessionUsageWire {
  id: string;
  created_at: number;
  updated_at: number;
  title: string | null;
  cost_usd: number;
  models: Record<string, number>;
  harness: string | null;
  other_harnesses: string[] | null;
  llm_model: string | null;
  agent_name: string | null;
}

interface UsageReportWire {
  cost_today: number;
  cost_last_7d: number;
  cost_last_30d: number;
  total_cost_usd: number;
  daily_costs: DailyCostWire[];
  sessions: SessionUsageWire[];
  sessions_has_more: boolean;
  sessions_last_id: string | null;
  harness_breakdown: Record<string, number>;
  model_breakdown: Record<string, number>;
}

// ── App types (camelCase) ───────────────────────────────────────

export interface DailyCost {
  day: string;
  costUsd: number;
}

export interface SessionUsage {
  id: string;
  createdAt: number;
  updatedAt: number;
  title: string | null;
  costUsd: number;
  models: Record<string, number>;
  harness: string | null;
  otherHarnesses: string[] | null;
  llmModel: string | null;
  agentName: string | null;
}

export interface UsageReport {
  costToday: number;
  costLast7d: number;
  costLast30d: number;
  totalCostUsd: number;
  dailyCosts: DailyCost[];
  sessions: SessionUsage[];
  sessionsHasMore: boolean;
  sessionsLastId: string | null;
  harnessBreakdown: Record<string, number>;
  modelBreakdown: Record<string, number>;
}

// ── Fetch ───────────────────────────────────────────────────────

export interface FetchUsageReportParams {
  limit?: number;
  after?: string | null;
}

export async function fetchUsageReport(params?: FetchUsageReportParams): Promise<UsageReport> {
  const searchParams = new URLSearchParams();
  if (params?.limit) searchParams.set("limit", String(params.limit));
  if (params?.after) searchParams.set("after", params.after);
  const query = searchParams.toString();
  const url = query ? `/v1/usage?${query}` : "/v1/usage";

  const res = await authenticatedFetch(url);
  if (!res.ok) throw new Error(`Usage fetch failed: ${res.status}`);
  const wire: UsageReportWire = await res.json();
  return {
    costToday: wire.cost_today,
    costLast7d: wire.cost_last_7d,
    costLast30d: wire.cost_last_30d,
    totalCostUsd: wire.total_cost_usd,
    dailyCosts: (wire.daily_costs ?? []).map((d) => ({ day: d.day, costUsd: d.cost_usd })),
    sessions: (wire.sessions ?? []).map((s) => ({
      id: s.id,
      createdAt: s.created_at,
      updatedAt: s.updated_at,
      title: s.title,
      costUsd: s.cost_usd,
      models: s.models ?? {},
      harness: s.harness ?? null,
      otherHarnesses: s.other_harnesses ?? null,
      llmModel: s.llm_model ?? null,
      agentName: s.agent_name ?? null,
    })),
    sessionsHasMore: wire.sessions_has_more ?? false,
    sessionsLastId: wire.sessions_last_id ?? null,
    harnessBreakdown: wire.harness_breakdown ?? {},
    modelBreakdown: wire.model_breakdown ?? {},
  };
}
