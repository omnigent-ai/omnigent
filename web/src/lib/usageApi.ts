/**
 * Typed client for the `/v1/usage` endpoint.
 * Mirrors `omnigent/server/routes/usage.py`.
 */

import { authenticatedFetch } from "./identity";

/** Token counters (+ optional cost) for a model bucket or the grand totals. */
export interface UsageCounters {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  total_cost_usd?: number;
}

/** Aggregated usage across the caller's accessible conversations. */
export interface UsageSummary {
  object: "usage";
  conversations: number;
  totals: UsageCounters;
  by_model: Record<string, UsageCounters>;
}

export async function getUsage(): Promise<UsageSummary> {
  const res = await authenticatedFetch("/v1/usage");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as UsageSummary;
}
