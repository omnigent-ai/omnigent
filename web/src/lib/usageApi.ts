import { authenticatedFetch } from "./identity";

// A session referenced by the pagination cursor was deleted between page
// fetches, so the server returns 400 `stale_cursor`. Thrown as a distinct type
// so the Usage page can restart pagination from the first page.
export class StaleCursorError extends Error {
  constructor() {
    super("Usage session cursor is stale");
    this.name = "StaleCursorError";
  }
}

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
  // Pre-pagination servers omit these fields entirely; the client distinguishes
  // their absence from a legitimately empty map to detect a legacy response.
  sessions_has_more?: boolean;
  sessions_last_id?: string | null;
  harness_breakdown?: Record<string, number>;
  model_breakdown?: Record<string, number>;
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
  // The server predates pagination/breakdowns, so the page must filter and
  // aggregate the complete response locally.
  legacy: boolean;
}

// ── Fetch ───────────────────────────────────────────────────────

export interface FetchUsageReportParams {
  limit?: number;
  after?: string | null;
  since?: string | null;
  until?: string | null;
}

export async function fetchUsageReport(params?: FetchUsageReportParams): Promise<UsageReport> {
  const searchParams = new URLSearchParams();
  if (params?.limit != null) searchParams.set("limit", String(params.limit));
  if (params?.after != null) searchParams.set("after", params.after);
  if (params?.since != null) searchParams.set("since", params.since);
  if (params?.until != null) searchParams.set("until", params.until);
  const query = searchParams.toString();
  const url = query ? `/v1/usage?${query}` : "/v1/usage";

  const res = await authenticatedFetch(url);
  if (!res.ok) {
    if (res.status === 400) {
      const body = (await res.json().catch(() => null)) as {
        error?: { code?: string };
      } | null;
      if (body?.error?.code === "stale_cursor") throw new StaleCursorError();
    }
    throw new Error(`Usage fetch failed: ${res.status}`);
  }
  const wire: UsageReportWire = await res.json();
  const legacy =
    wire.sessions_has_more === undefined &&
    wire.harness_breakdown === undefined &&
    wire.model_breakdown === undefined;
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
    legacy,
  };
}

// ── Legacy-response aggregation ─────────────────────────────────
// Mirrors the server's first-page aggregation for pre-pagination servers;
// sessions without a resolved harness bucket under "Unknown".

export function aggregateHarnessBreakdown(sessions: SessionUsage[]): Record<string, number> {
  // Null prototype so a harness named "__proto__" counts as a key instead of
  // hitting Object.prototype's setter.
  const breakdown: Record<string, number> = Object.create(null);
  for (const session of sessions) {
    const harness = session.harness ?? "Unknown";
    breakdown[harness] = (breakdown[harness] ?? 0) + session.costUsd;
  }
  return breakdown;
}

export function aggregateModelBreakdown(sessions: SessionUsage[]): Record<string, number> {
  const breakdown: Record<string, number> = Object.create(null);
  for (const session of sessions) {
    for (const [model, cost] of Object.entries(session.models)) {
      breakdown[model] = (breakdown[model] ?? 0) + cost;
    }
  }
  return breakdown;
}
