import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useGoalState } from "@/components/goal/useGoalState";
import {
  clearCodexGoal,
  codexGoalApiErrorFromResponse,
  getCodexGoal,
  setCodexGoal,
  updateCodexGoalStatus,
} from "./codexGoalApi";
import { authenticatedFetch } from "./identity";

vi.mock("./identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const mockAuthenticatedFetch = vi.mocked(authenticatedFetch);

function mockJsonResponse(
  body: unknown,
  init: { ok?: boolean; status?: number; statusText?: string; headers?: HeadersInit } = {},
): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.statusText ?? "OK",
    headers: new Headers(init.headers),
    json: async () => body,
  } as unknown as Response;
}

const WIRE_GOAL = {
  thread_id: "thread-1",
  objective: "Ship goal mode",
  status: "budgetLimited",
  token_budget: 40000,
  tokens_used: 1200,
  time_used_seconds: 125,
  created_at: 1,
  updated_at: 2,
};

afterEach(() => {
  vi.useRealTimers();
});

describe("codex goal API", () => {
  beforeEach(() => {
    mockAuthenticatedFetch.mockReset();
  });

  it("reads and converts a populated goal response from an older server", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal: WIRE_GOAL }));

    await expect(getCodexGoal("conv a/b")).resolves.toEqual({
      goal: {
        threadId: "thread-1",
        objective: "Ship goal mode",
        status: "budgetLimited",
        tokenBudget: 40000,
        tokensUsed: 1200,
        timeUsedSeconds: 125,
        createdAt: 1,
        updatedAt: 2,
      },
    });
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv%20a%2Fb/codex_goal");
  });

  it("accepts a confirmed empty goal after an external deletion", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse({ goal: null }, { headers: { "X-Omnigent-Codex-Goal-Read": "confirmed" } }),
    );

    await expect(getCodexGoal("conv")).resolves.toEqual({ goal: null });
  });

  it("accepts an empty goal from an older server without a confirmation header", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal: null }));

    await expect(getCodexGoal("conv")).resolves.toEqual({ goal: null });
  });

  it("clears an externally deleted legacy goal after retaining it through a read failure", async () => {
    vi.useFakeTimers();
    mockAuthenticatedFetch
      .mockResolvedValueOnce(mockJsonResponse({ goal: WIRE_GOAL }))
      .mockResolvedValueOnce(
        mockJsonResponse(
          { error: "codex_native_goal_failed", detail: "runner unavailable" },
          { ok: false, status: 503, statusText: "Service Unavailable" },
        ),
      )
      .mockResolvedValueOnce(mockJsonResponse({ goal: null }));
    const { result } = renderHook(() => useGoalState("conv", true));
    await act(async () => {});
    const initialGoal = result.current.goal;
    expect(initialGoal?.objective).toBe(WIRE_GOAL.objective);

    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(result.current.goal).toEqual(initialGoal);
    expect(mockAuthenticatedFetch).toHaveBeenCalledTimes(2);

    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(result.current.goal).toBeNull();
    expect(mockAuthenticatedFetch).toHaveBeenCalledTimes(3);
  });

  it("sets, updates, and clears goals with the expected wire payloads", async () => {
    mockAuthenticatedFetch
      .mockResolvedValueOnce(mockJsonResponse({ goal: WIRE_GOAL }))
      .mockResolvedValueOnce(mockJsonResponse({ goal: { ...WIRE_GOAL, status: "paused" } }))
      .mockResolvedValueOnce(mockJsonResponse({ cleared: true }));

    await setCodexGoal("conv", { objective: "Do it", tokenBudget: null, status: "active" });
    await updateCodexGoalStatus("conv", "paused");
    await expect(clearCodexGoal("conv")).resolves.toEqual({ cleared: true });

    expect(mockAuthenticatedFetch).toHaveBeenNthCalledWith(
      1,
      "/v1/sessions/conv/codex_goal",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ objective: "Do it", token_budget: null, status: "active" }),
      }),
    );
    expect(mockAuthenticatedFetch).toHaveBeenNthCalledWith(
      2,
      "/v1/sessions/conv/codex_goal/status",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ status: "paused" }) }),
    );
    expect(mockAuthenticatedFetch).toHaveBeenNthCalledWith(
      3,
      "/v1/sessions/conv/codex_goal",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("reads standard nested Omnigent error envelopes", async () => {
    const err = await codexGoalApiErrorFromResponse(
      mockJsonResponse(
        { error: { code: "codex_native_goal_failed", message: "runner is asleep" } },
        { ok: false, status: 503, statusText: "Service Unavailable" },
      ),
    );

    expect(err.status).toBe(503);
    expect(err.code).toBe("codex_native_goal_failed");
    expect(err.message).toBe("runner is asleep");
  });

  it("reads flat runner error envelopes preserved by the AP route", async () => {
    const err = await codexGoalApiErrorFromResponse(
      mockJsonResponse(
        { error: "invalid_input", detail: "harness mismatch" },
        { ok: false, status: 400, statusText: "Bad Request" },
      ),
    );

    expect(err.status).toBe(400);
    expect(err.code).toBe("invalid_input");
    expect(err.message).toBe("harness mismatch");
  });
});
