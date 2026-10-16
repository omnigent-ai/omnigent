// Unit tests for `usageApi.ts` — the camelCase TS ↔ snake_case wire mapping,
// the typed stale-cursor error the paginated Usage page recovers from, and the
// legacy-server fallback that aggregates a pre-pagination response locally.

import { afterEach, describe, expect, it, vi } from "vitest";
import * as identity from "./identity";
import {
  aggregateHarnessBreakdown,
  aggregateModelBreakdown,
  fetchUsageReport,
  StaleCursorError,
  type SessionUsage,
} from "./usageApi";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

function session(overrides: Partial<SessionUsage>): SessionUsage {
  return {
    id: "conv",
    createdAt: 0,
    updatedAt: 0,
    title: null,
    costUsd: 0,
    models: {},
    harness: null,
    otherHarnesses: null,
    llmModel: null,
    agentName: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("fetchUsageReport", () => {
  it("maps the snake_case wire payload to camelCase", async () => {
    const fetchSpy = vi.spyOn(identity, "authenticatedFetch").mockResolvedValue(
      mockResponse({
        cost_today: 1,
        cost_last_7d: 2,
        cost_last_30d: 3,
        total_cost_usd: 4,
        daily_costs: [{ day: "2026-07-22", cost_usd: 1.5 }],
        sessions: [
          {
            id: "conv_1",
            created_at: 10,
            updated_at: 20,
            title: "s",
            cost_usd: 1.5,
            models: { "model-a": 1.5 },
            harness: "codex-native",
            other_harnesses: null,
            llm_model: "model-a",
            agent_name: null,
          },
        ],
        sessions_has_more: true,
        sessions_last_id: "conv_1",
        harness_breakdown: { "codex-native": 1.5 },
        model_breakdown: { "model-a": 1.5 },
      }),
    );

    const report = await fetchUsageReport({ limit: 50, after: "conv_0" });

    expect(fetchSpy).toHaveBeenCalledWith("/v1/usage?limit=50&after=conv_0");
    expect(report.totalCostUsd).toBe(4);
    expect(report.dailyCosts).toEqual([{ day: "2026-07-22", costUsd: 1.5 }]);
    expect(report.sessions[0].costUsd).toBe(1.5);
    expect(report.sessionsHasMore).toBe(true);
    expect(report.sessionsLastId).toBe("conv_1");
    expect(report.harnessBreakdown).toEqual({ "codex-native": 1.5 });
    expect(report.modelBreakdown).toEqual({ "model-a": 1.5 });
    expect(report.legacy).toBe(false);
  });

  it("flags a pre-pagination server response as legacy", async () => {
    vi.spyOn(identity, "authenticatedFetch").mockResolvedValue(
      mockResponse({
        cost_today: 0,
        cost_last_7d: 0,
        cost_last_30d: 0,
        total_cost_usd: 4,
        daily_costs: [],
        sessions: [
          {
            id: "conv_1",
            created_at: 10,
            updated_at: 20,
            title: "s",
            cost_usd: 4,
            models: { "model-a": 4 },
            harness: "codex-native",
            other_harnesses: null,
            llm_model: "model-a",
            agent_name: null,
          },
        ],
        // No sessions_has_more / sessions_last_id / *_breakdown fields.
      }),
    );

    const report = await fetchUsageReport({ limit: 50, since: "2026-01-01" });

    expect(report.legacy).toBe(true);
    expect(report.sessionsHasMore).toBe(false);
    expect(report.sessionsLastId).toBeNull();
    expect(report.harnessBreakdown).toEqual({});
    expect(report.modelBreakdown).toEqual({});
  });

  it("treats an empty breakdown map from a current server as non-legacy", async () => {
    vi.spyOn(identity, "authenticatedFetch").mockResolvedValue(
      mockResponse({
        cost_today: 0,
        cost_last_7d: 0,
        cost_last_30d: 0,
        total_cost_usd: 0,
        daily_costs: [],
        sessions: [],
        sessions_has_more: false,
        sessions_last_id: null,
        harness_breakdown: {},
        model_breakdown: {},
      }),
    );

    const report = await fetchUsageReport({ limit: 50 });

    expect(report.legacy).toBe(false);
  });

  it("throws StaleCursorError when a next-page cursor row vanished", async () => {
    vi.spyOn(identity, "authenticatedFetch").mockResolvedValue(
      mockResponse(
        { error: { code: "stale_cursor", message: "pagination cursor gone" } },
        { ok: false, status: 400 },
      ),
    );

    await expect(fetchUsageReport({ limit: 50, after: "conv_gone" })).rejects.toBeInstanceOf(
      StaleCursorError,
    );
  });

  it("throws a generic error for other non-OK responses", async () => {
    vi.spyOn(identity, "authenticatedFetch").mockResolvedValue(
      mockResponse({ error: { code: "internal_error" } }, { ok: false, status: 500 }),
    );

    const rejection = fetchUsageReport();
    await expect(rejection).rejects.toThrow("Usage fetch failed: 500");
    await expect(fetchUsageReport()).rejects.not.toBeInstanceOf(StaleCursorError);
  });
});

describe("legacy-response aggregation", () => {
  it("buckets an unresolved harness under Unknown", () => {
    const breakdown = aggregateHarnessBreakdown([
      session({ harness: "codex-native", costUsd: 1.5 }),
      session({ harness: null, costUsd: 2.5 }),
      session({ harness: "codex-native", costUsd: 0.5 }),
    ]);

    expect(breakdown).toEqual({ "codex-native": 2, Unknown: 2.5 });
  });

  it("sums per-model cost across sessions", () => {
    const breakdown = aggregateModelBreakdown([
      session({ models: { "model-a": 1, "model-b": 2 } }),
      session({ models: { "model-a": 3 } }),
    ]);

    expect(breakdown).toEqual({ "model-a": 4, "model-b": 2 });
  });
});
