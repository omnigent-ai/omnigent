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
  project_id: string | null;
  project_name: string | null;
}

interface ProjectCostWire {
  project_id: string | null;
  project_name: string | null;
  cost_usd: number;
  session_count: number;
}

interface CostAlertWire {
  id: string;
  threshold_usd: number;
  period: string;
  enabled: boolean;
  created_at: number;
}

interface UsageForecastWire {
  projected_cost_30d: number;
  projected_daily: DailyCostWire[];
  trend: string;
}

interface UsageReportWire {
  cost_today: number;
  cost_last_7d: number;
  cost_last_30d: number;
  total_cost_usd: number;
  daily_costs: DailyCostWire[];
  sessions: SessionUsageWire[];
  projects: ProjectCostWire[];
  alerts: CostAlertWire[];
  alert_triggered: boolean;
  forecast: UsageForecastWire | null;
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
  projectId: string | null;
  projectName: string | null;
}

export interface ProjectCost {
  projectId: string | null;
  projectName: string | null;
  costUsd: number;
  sessionCount: number;
}

export interface CostAlert {
  id: string;
  thresholdUsd: number;
  period: string;
  enabled: boolean;
  createdAt: number;
}

export interface UsageForecast {
  projectedCost30d: number;
  projectedDaily: DailyCost[];
  trend: string;
}

export interface UsageReport {
  costToday: number;
  costLast7d: number;
  costLast30d: number;
  totalCostUsd: number;
  dailyCosts: DailyCost[];
  sessions: SessionUsage[];
  projects: ProjectCost[];
  alerts: CostAlert[];
  alertTriggered: boolean;
  forecast: UsageForecast | null;
}

// ── Fetch ───────────────────────────────────────────────────────

export async function fetchUsageReport(params?: {
  since?: string | null;
  until?: string | null;
}): Promise<UsageReport> {
  const searchParams = new URLSearchParams();
  if (params?.since) searchParams.set("since", params.since);
  if (params?.until) searchParams.set("until", params.until);
  const qs = searchParams.toString();
  const url = qs ? `/v1/usage?${qs}` : "/v1/usage";
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
      projectId: s.project_id ?? null,
      projectName: s.project_name ?? null,
    })),
    projects: (wire.projects ?? []).map((p) => ({
      projectId: p.project_id ?? null,
      projectName: p.project_name ?? null,
      costUsd: p.cost_usd,
      sessionCount: p.session_count,
    })),
    alerts: (wire.alerts ?? []).map((a) => ({
      id: a.id,
      thresholdUsd: a.threshold_usd,
      period: a.period,
      enabled: a.enabled,
      createdAt: a.created_at,
    })),
    alertTriggered: wire.alert_triggered ?? false,
    forecast: wire.forecast
      ? {
          projectedCost30d: wire.forecast.projected_cost_30d,
          projectedDaily: (wire.forecast.projected_daily ?? []).map((d) => ({
            day: d.day,
            costUsd: d.cost_usd,
          })),
          trend: wire.forecast.trend,
        }
      : null,
  };
}

// ── Alert CRUD ──────────────────────────────────────────────────

export async function createCostAlert(thresholdUsd: number, period: string): Promise<CostAlert> {
  const res = await authenticatedFetch("/v1/usage/alerts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ threshold_usd: thresholdUsd, period }),
  });
  if (!res.ok) throw new Error(`Create alert failed: ${res.status}`);
  const wire: CostAlertWire = await res.json();
  return {
    id: wire.id,
    thresholdUsd: wire.threshold_usd,
    period: wire.period,
    enabled: wire.enabled,
    createdAt: wire.created_at,
  };
}

export async function deleteCostAlert(alertId: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/usage/alerts/${alertId}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Delete alert failed: ${res.status}`);
}

export async function updateCostAlert(
  alertId: string,
  updates: { enabled?: boolean; threshold_usd?: number },
): Promise<CostAlert> {
  const res = await authenticatedFetch(`/v1/usage/alerts/${alertId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!res.ok) throw new Error(`Update alert failed: ${res.status}`);
  const wire: CostAlertWire = await res.json();
  return {
    id: wire.id,
    thresholdUsd: wire.threshold_usd,
    period: wire.period,
    enabled: wire.enabled,
    createdAt: wire.created_at,
  };
}
