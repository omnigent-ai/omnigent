import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearCodexGoal, getCodexGoal, setCodexGoal, updateCodexGoalStatus } from "./codexGoalApi";
import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

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
  { name: "strict read", timeoutMs: 60_000, request: () => getCodexGoal("conv") },
  {
    name: "background read",
    timeoutMs: 60_000,
    request: () => getCodexGoal("conv", { strict: false }),
  },
  {
    name: "save",
    timeoutMs: 190_000,
    request: () => setCodexGoal("conv", { objective: "Do it" }),
  },
  { name: "pause", timeoutMs: 190_000, request: () => updateCodexGoalStatus("conv", "paused") },
  { name: "resume", timeoutMs: 190_000, request: () => updateCodexGoalStatus("conv", "active") },
  { name: "clear", timeoutMs: 190_000, request: () => clearCodexGoal("conv") },
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

  it.each(
    [
      {
        mode: "default strict",
        options: undefined,
        header: "strict",
        legacyResult: { goal: null },
      },
      {
        mode: "explicit strict",
        options: { strict: true },
        header: "strict",
        legacyResult: { goal: null },
      },
      {
        mode: "background",
        options: { strict: false },
        header: null,
        legacyResult: { available: false },
      },
    ].flatMap(({ legacyResult, ...mode }) => [
      { ...mode, envelope: "legacy", body: { goal: null }, expected: legacyResult },
      {
        ...mode,
        envelope: "confirmed",
        body: { goal: null, available: true },
        expected: { goal: null },
      },
    ]),
  )("reads $envelope null in $mode mode", async ({ options, body, expected, header }) => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse(body));

    await expect(getCodexGoal("conv", options)).resolves.toEqual(expected);

    const [url, init] = mockAuthenticatedFetch.mock.calls[0];
    expect(url).toBe("/v1/sessions/conv/codex_goal");
    expect(new Headers(init?.headers).get("X-Omnigent-Codex-Goal-Read")).toBe(header);
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("keeps an unavailable background read distinct from an absent goal", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse({ goal: null, available: false }),
    );

    await expect(getCodexGoal("conv", { strict: false })).resolves.toEqual({
      available: false,
    });
    expect(
      new Headers(mockAuthenticatedFetch.mock.calls[0][1]?.headers).has(
        "X-Omnigent-Codex-Goal-Read",
      ),
    ).toBe(false);
  });

  it("accepts an unmarked populated background goal", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal: WIRE_GOAL }));

    const response = await getCodexGoal("conv", { strict: false });

    expect(response).toMatchObject({
      goal: { threadId: "thread-1", objective: WIRE_GOAL.objective },
    });
    expect(response).not.toHaveProperty("available");
  });

  it.each([undefined, null, 0, 1.5])(
    "accepts unknown statuses, fractional time, and optional numbers %s",
    async (optionalNumber) => {
      const goal = {
        thread_id: WIRE_GOAL.thread_id,
        objective: WIRE_GOAL.objective,
        status: "futureNativeStatus",
        tokens_used: 0,
        time_used_seconds: 0.5,
        ...(optionalNumber === undefined
          ? {}
          : {
              token_budget: optionalNumber,
              created_at: optionalNumber,
              updated_at: optionalNumber,
            }),
      };
      mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal }));

      await expect(getCodexGoal("conv")).resolves.toEqual({
        goal: {
          threadId: goal.thread_id,
          objective: goal.objective,
          status: goal.status,
          tokenBudget: optionalNumber ?? null,
          tokensUsed: 0,
          timeUsedSeconds: 0.5,
          createdAt: optionalNumber ?? null,
          updatedAt: optionalNumber ?? null,
        },
      });
    },
  );

  it.each([undefined, true])("rejects unavailable strict reads with strict=%s", async (strict) => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse({ goal: null, available: false }),
    );

    await expect(getCodexGoal("conv", { strict })).rejects.toMatchObject({
      status: 503,
      code: "codex_native_goal_failed",
      message: "Could not read Codex goal: no live native goal response is available.",
    });
  });

  it.each([null, 0, 1, "true", "false", [], {}])(
    "rejects malformed read availability %j without falling back to legacy null",
    async (available) => {
      mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal: null, available }));

      await expect(getCodexGoal("conv", { strict: false })).rejects.toMatchObject({ status: 502 });
    },
  );

  it("rejects an unavailable read that carries a non-null goal", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse({ goal: WIRE_GOAL, available: false }),
    );

    await expect(getCodexGoal("conv", { strict: false })).rejects.toMatchObject({ status: 502 });
  });

  it.each([null, true, 1, "bad", [], {}, { available: true }, { available: false }])(
    "rejects a malformed read envelope %j",
    async (body) => {
      mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse(body));

      await expect(getCodexGoal("conv", { strict: false })).rejects.toMatchObject({
        status: 502,
        code: "codex_native_goal_failed",
        message: "Could not read Codex goal: server returned a malformed response.",
      });
    },
  );

  it.each([
    undefined,
    true,
    1,
    "bad",
    [],
    {},
    ...["thread_id", "objective", "status"].map((field) => ({ ...WIRE_GOAL, [field]: null })),
    ...["tokens_used", "time_used_seconds", "token_budget", "created_at", "updated_at"].flatMap(
      (field) => ["bad", {}, NaN, Infinity].map((value) => ({ ...WIRE_GOAL, [field]: value })),
    ),
    { ...WIRE_GOAL, tokens_used: undefined },
    { ...WIRE_GOAL, time_used_seconds: undefined },
  ])("rejects a malformed nested read goal %j", async (goal) => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal }));

    const response = getCodexGoal("conv");

    await expect(response).rejects.toBeInstanceOf(ApiError);
    await expect(response).rejects.toMatchObject({
      status: 502,
      code: "codex_native_goal_failed",
    });
  });

  it.each(goalRequests.filter(({ name }) => ["save", "pause"].includes(name)))(
    "rejects a malformed nested mutation goal for $name",
    async ({ request }) => {
      mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ goal: [] }));

      await expect(request()).rejects.toMatchObject({
        status: 502,
        code: "codex_native_goal_failed",
      });
    },
  );

  it.each(
    goalRequests
      .filter(({ name }) => ["save", "pause", "clear"].includes(name))
      .flatMap((operation) =>
        [null, true, {}, ...(operation.name === "clear" ? [{ cleared: "false" }] : [])].map(
          (body) => ({ ...operation, body }),
        ),
      ),
  )("rejects a malformed mutation response for $name: $body", async ({ request, body }) => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse(body));

    const response = request();

    await expect(response).rejects.toBeInstanceOf(ApiError);
    await expect(response).rejects.toMatchObject({
      status: 502,
      code: "codex_native_goal_failed",
      message: "Codex goal request failed: server returned a malformed response.",
    });
  });

  it("keeps read availability out of mutation conversion", async () => {
    mockAuthenticatedFetch.mockResolvedValue(
      mockJsonResponse({ goal: WIRE_GOAL, available: false }),
    );

    for (const response of [
      await setCodexGoal("conv", { objective: "Ship goal mode" }),
      await updateCodexGoalStatus("conv", "paused"),
    ]) {
      expect(response.goal?.objective).toBe(WIRE_GOAL.objective);
      expect(response).not.toHaveProperty("available");
    }
  });

  it.each([WIRE_GOAL, null])(
    "sets, updates, and clears goals with the expected wire payloads for %j",
    async (goal) => {
      const cleared = goal !== null;
      mockAuthenticatedFetch
        .mockResolvedValueOnce(mockJsonResponse({ goal }))
        .mockResolvedValueOnce(
          mockJsonResponse({ goal: goal === null ? null : { ...goal, status: "paused" } }),
        )
        .mockResolvedValueOnce(mockJsonResponse({ cleared }));

      await expect(
        setCodexGoal("conv", { objective: "Do it", tokenBudget: null, status: "active" }),
      ).resolves.toMatchObject({
        goal: goal === null ? null : { objective: goal.objective },
      });
      await expect(updateCodexGoalStatus("conv", "paused")).resolves.toMatchObject({
        goal: goal === null ? null : { status: "paused" },
      });
      await expect(clearCodexGoal("conv")).resolves.toEqual({ cleared });

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
    },
  );

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
      goalRequests
        .filter(({ name }) => name !== "resume")
        .flatMap((request) => ["resolve", "reject"].map((late) => ({ ...request, late }))),
    )(
      "bounds $name and ignores a late $late after timeout",
      async ({ request, late, timeoutMs }) => {
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
          await vi.advanceTimersByTimeAsync(timeoutMs - 1);
          expect(fulfilled).not.toHaveBeenCalled();
          expect(rejected).not.toHaveBeenCalled();
          expect(signal?.aborted ?? false).toBe(false);

          await vi.advanceTimersByTimeAsync(1);

          expect(rejected).toHaveBeenCalledTimes(1);
          const error = rejected.mock.calls[0][0] as ApiError;
          expect(error).toBeInstanceOf(ApiError);
          expect(error.status).toBe(504);
          expect(error.code).toBe("codex_goal_request_timeout");
          expect(error.message).toMatch(/timed out/i);
          if (timeoutMs === 60_000) {
            expect(error.message).toBe("Goal read timed out. Try again.");
            expect(error.message).not.toMatch(/uncertain/i);
          } else {
            expect(error.message).toMatch(/uncertain/i);
            expect(error.message).toMatch(/refresh.*before.*retry/i);
          }
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
        await vi.advanceTimersByTimeAsync(timeoutMs);
        expect(mockAuthenticatedFetch).toHaveBeenCalledTimes(1);
        expect(vi.getTimerCount()).toBe(0);
      },
    );
  });

  it.each(goalRequests.filter(({ timeoutMs }) => timeoutMs > 60_000))(
    "allows a cold-runner $name to finish after the old one-minute deadline",
    async ({ request }) => {
      vi.useFakeTimers();
      const pending = deferred<Response>();
      mockAuthenticatedFetch.mockReturnValueOnce(pending.promise);
      const fulfilled = vi.fn();
      const rejected = vi.fn();
      const outcome = request().then(fulfilled, rejected);
      const signal = mockAuthenticatedFetch.mock.calls[0][1]?.signal;

      await vi.advanceTimersByTimeAsync(75_000);

      expect(fulfilled).not.toHaveBeenCalled();
      expect(rejected).not.toHaveBeenCalled();
      expect(signal?.aborted).toBe(false);
      pending.resolve(mockJsonResponse({ goal: WIRE_GOAL, cleared: true }));
      await outcome;
      expect(fulfilled).toHaveBeenCalledTimes(1);
      expect(rejected).not.toHaveBeenCalled();
      expect(mockAuthenticatedFetch).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(0);
    },
  );

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

    await vi.advanceTimersByTimeAsync(190_000);

    expect(rejected).toHaveBeenCalledWith(
      expect.objectContaining({
        status: 504,
        code: "codex_goal_request_timeout",
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
