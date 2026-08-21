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
}

// ── Cost Breakdown types ────────────────────────────────────────

interface UsageEntryWire {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  total_cost_usd?: number;
}

interface CategoryBreakdownItemWire {
  name: string;
  usage: UsageEntryWire;
}

interface CategoryBreakdownWire {
  total: UsageEntryWire;
  items: CategoryBreakdownItemWire[];
}

interface SessionCostBreakdownWire {
  session_id: string;
  total_cost_usd: number;
  total_tokens: number;
  tools: CategoryBreakdownWire;
  shell: CategoryBreakdownWire;
  model: CategoryBreakdownWire;
  system: CategoryBreakdownWire;
  user: CategoryBreakdownWire;
  images: CategoryBreakdownWire;
}

export interface UsageEntry {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadInputTokens: number;
  cacheCreationInputTokens: number;
  totalCostUsd?: number;
}

export interface CategoryBreakdownItem {
  name: string;
  usage: UsageEntry;
}

export interface CategoryBreakdown {
  total: UsageEntry;
  items: CategoryBreakdownItem[];
}

export interface SessionCostBreakdown {
  sessionId: string;
  totalCostUsd: number;
  totalTokens: number;
  tools: CategoryBreakdown;
  shell: CategoryBreakdown;
  model: CategoryBreakdown;
  system: CategoryBreakdown;
  user: CategoryBreakdown;
  images: CategoryBreakdown;
}

// ── Fetch ───────────────────────────────────────────────────────

export async function fetchUsageReport(): Promise<UsageReport> {
  const res = await authenticatedFetch("/v1/usage");
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
  };
}

function convertUsageEntry(wire: UsageEntryWire): UsageEntry {
  return {
    inputTokens: wire.input_tokens,
    outputTokens: wire.output_tokens,
    totalTokens: wire.total_tokens,
    cacheReadInputTokens: wire.cache_read_input_tokens,
    cacheCreationInputTokens: wire.cache_creation_input_tokens,
    totalCostUsd: wire.total_cost_usd,
  };
}

function convertCategoryBreakdown(wire: CategoryBreakdownWire): CategoryBreakdown {
  return {
    total: convertUsageEntry(wire.total),
    items: (wire.items ?? []).map((item) => ({
      name: item.name,
      usage: convertUsageEntry(item.usage),
    })),
  };
}

export async function fetchSessionCostBreakdown(
  sessionId: string
): Promise<SessionCostBreakdown> {
  const res = await authenticatedFetch(`/v1/sessions/${sessionId}/cost-breakdown`);
  if (!res.ok) throw new Error(`Cost breakdown fetch failed: ${res.status}`);
  const wire: SessionCostBreakdownWire = await res.json();
  return {
    sessionId: wire.session_id,
    totalCostUsd: wire.total_cost_usd,
    totalTokens: wire.total_tokens,
    tools: convertCategoryBreakdown(wire.tools),
    shell: convertCategoryBreakdown(wire.shell),
    model: convertCategoryBreakdown(wire.model),
    system: convertCategoryBreakdown(wire.system),
    user: convertCategoryBreakdown(wire.user),
    images: convertCategoryBreakdown(wire.images),
  };
}
