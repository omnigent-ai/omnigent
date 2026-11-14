import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearCodexGoal, getCodexGoal, setCodexGoal, updateCodexGoalStatus } from "./codexGoalApi";
import { authenticatedFetch } from "./identity";

vi.mock("./identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const mockAuthenticatedFetch = vi.mocked(authenticatedFetch);

function mockJsonResponse(
  body: unknown,
  init: { ok?: boolean; status?: number; statusText?: string } = {},
): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.statusText ?? "OK",
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const goalRequests = [
  { name: "strict read", request: () => getCodexGoal("conv") },
  { name: "background read", request: () => getCodexGoal("conv", { strict: false }) },
  { name: "save", request: () => setCodexGoal("conv", { objective: "Do it" }) },
  { name: "pause", request: () => updateCodexGoalStatus("conv", "paused") },
  { name: "resume", request: () => updateCodexGoalStatus("conv", "active") },
  { name: "clear", request: () => clearCodexGoal("conv") },
];

describe("codex goal API", () => {
  beforeEach(() => {
    mockAuthenticatedFetch.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("reads and converts a populated goal response", async () => {
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
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv%20a%2Fb/codex_goal", {
      headers: { "X-Omnigent-Codex-Goal-Read": "strict" },
      signal: expect.any(AbortSignal),
    });
  });

  it("returns goal: null when the server reports no goal", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal: null }));

    await expect(getCodexGoal("conv")).resolves.toEqual({ goal: null });
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv/codex_goal", {
      headers: { "X-Omnigent-Codex-Goal-Read": "strict" },
      signal: expect.any(AbortSignal),
    });
  });

  it("supports explicitly strict reads", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal: null }));

    await expect(getCodexGoal("conv", { strict: true })).resolves.toEqual({ goal: null });

    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv/codex_goal", {
      headers: { "X-Omnigent-Codex-Goal-Read": "strict" },
      signal: expect.any(AbortSignal),
    });
  });

  it("allows an idle background read to return no goal without requiring a live runner", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal: null }));

    await expect(getCodexGoal("conv", { strict: false })).resolves.toEqual({ goal: null });

    const [url, init] = mockAuthenticatedFetch.mock.calls[0];
    expect(url).toBe("/v1/sessions/conv/codex_goal");
    expect(new Headers(init?.headers).has("X-Omnigent-Codex-Goal-Read")).toBe(false);
    expect(init?.signal).toBeInstanceOf(AbortSignal);
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

  it.each([502, 503])("rejects a strict goal read with nested status %s", async (status) => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse(
        { error: { code: "codex_native_goal_failed", message: "runner is asleep" } },
        { ok: false, status, statusText: "Runner Unavailable" },
      ),
    );

    await expect(getCodexGoal("conv")).rejects.toMatchObject({
      status,
      code: "codex_native_goal_failed",
      message: "runner is asleep",
    });
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv/codex_goal", {
      headers: { "X-Omnigent-Codex-Goal-Read": "strict" },
      signal: expect.any(AbortSignal),
    });
  });

  it.each([400, 409])("rejects a strict goal read with flat runner status %s", async (status) => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse(
        { error: "invalid_input", detail: "harness mismatch" },
        { ok: false, status, statusText: "Request Rejected" },
      ),
    );

    await expect(getCodexGoal("conv")).rejects.toMatchObject({
      status,
      code: "invalid_input",
      message: "harness mismatch",
    });
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv/codex_goal", {
      headers: { "X-Omnigent-Codex-Goal-Read": "strict" },
      signal: expect.any(AbortSignal),
    });
  });

  it.each([502, 503])("still rejects a background read failure with status %s", async (status) => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse(
        { error: { code: "codex_native_goal_failed", message: "read failed" } },
        { ok: false, status },
      ),
    );

    await expect(getCodexGoal("conv", { strict: false })).rejects.toMatchObject({
      status,
      code: "codex_native_goal_failed",
      message: "read failed",
    });
    expect(
      new Headers(mockAuthenticatedFetch.mock.calls[0][1]?.headers).has(
        "X-Omnigent-Codex-Goal-Read",
      ),
    ).toBe(false);
  });

  describe.each(["fetch", "success body", "error body"])("stalled %s", (stage) => {
    it.each(
      goalRequests.flatMap((request) =>
        ["resolve", "reject"].map((late) => ({ ...request, late })),
      ),
    )("bounds $name and ignores a late $late after timeout", async ({ request, late }) => {
      vi.useFakeTimers();
      const stalled = deferred<unknown>();
      const payload =
        stage === "error body"
          ? { error: { code: "codex_native_goal_failed", message: "late server error" } }
          : { goal: WIRE_GOAL, cleared: true };
      const response = mockJsonResponse(payload, {
        ok: stage !== "error body",
        status: stage === "error body" ? 503 : 200,
      });
      if (stage === "fetch") {
        mockAuthenticatedFetch.mockReturnValueOnce(stalled.promise as Promise<Response>);
      } else {
        response.json = vi.fn(() => stalled.promise);
        mockAuthenticatedFetch.mockResolvedValueOnce(response);
      }
      const fulfilled = vi.fn();
      const rejected = vi.fn();
      const outcome = request().then(fulfilled, rejected);
      const signal = mockAuthenticatedFetch.mock.calls[0][1]?.signal;
      try {
        await vi.advanceTimersByTimeAsync(59_999);
        expect(fulfilled).not.toHaveBeenCalled();
        expect(rejected).not.toHaveBeenCalled();
        expect(signal?.aborted ?? false).toBe(false);

        await vi.advanceTimersByTimeAsync(1);

        expect(rejected).toHaveBeenCalledTimes(1);
        const error = rejected.mock.calls[0][0] as Error;
        expect(error.message).toMatch(/timed out/i);
        expect(error.message).toMatch(/uncertain/i);
        expect(error.message).toMatch(/refresh.*before.*retry/i);
        expect(signal).toBeInstanceOf(AbortSignal);
        expect(signal?.aborted).toBe(true);
        expect(vi.getTimerCount()).toBe(0);
      } finally {
        if (late === "reject") stalled.reject(new Error("late transport or body failure"));
        else stalled.resolve(stage === "fetch" ? response : payload);
        await vi.advanceTimersByTimeAsync(0);
        await outcome;
      }

      expect(fulfilled).not.toHaveBeenCalled();
      expect(rejected).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(60_000);
      expect(mockAuthenticatedFetch).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(0);
    });
  });

  it("keeps the uncertain-outcome message when fetch rejects on abort", async () => {
    vi.useFakeTimers();
    mockAuthenticatedFetch.mockImplementationOnce(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), {
            once: true,
          });
        }),
    );
    const rejected = vi.fn();
    const outcome = setCodexGoal("conv", { objective: "Do it" }).catch(rejected);

    await vi.advanceTimersByTimeAsync(60_000);

    expect(rejected).toHaveBeenCalledWith(
      expect.objectContaining({
        message: expect.stringMatching(/uncertain.*refresh.*before.*retry/i),
      }),
    );
    await outcome;
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(goalRequests)("clears the deadline after a successful $name", async ({ request }) => {
    vi.useFakeTimers();
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse({ goal: WIRE_GOAL, cleared: true }),
    );

    await request();

    const signal = mockAuthenticatedFetch.mock.calls[0][1]?.signal;
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal?.aborted).toBe(false);
    expect(mockAuthenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it.each(["network", "JSON", "HTTP"])(
    "clears the deadline after an early %s error",
    async (stage) => {
      vi.useFakeTimers();
      const error = new Error("early failure");
      if (stage === "network") mockAuthenticatedFetch.mockRejectedValueOnce(error);
      else {
        const response = mockJsonResponse(
          { error: { code: "codex_native_goal_failed", message: error.message } },
          { ok: stage !== "HTTP", status: stage === "HTTP" ? 503 : 200 },
        );
        if (stage === "JSON") response.json = vi.fn().mockRejectedValueOnce(error);
        mockAuthenticatedFetch.mockResolvedValueOnce(response);
      }

      await expect(setCodexGoal("conv", { objective: "Do it" })).rejects.toThrow("early failure");

      expect(vi.getTimerCount()).toBe(0);
      await vi.advanceTimersByTimeAsync(60_000);
      expect(mockAuthenticatedFetch.mock.calls[0][1]?.signal?.aborted).toBe(false);
      expect(mockAuthenticatedFetch).toHaveBeenCalledTimes(1);
    },
  );
});
