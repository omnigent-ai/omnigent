import { beforeEach, describe, expect, it, vi } from "vitest";
import { getClaudeGoal } from "./claudeGoalApi";
import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

vi.mock("./identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const mockAuthenticatedFetch = vi.mocked(authenticatedFetch);

function mockJsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

describe("getClaudeGoal", () => {
  beforeEach(() => {
    mockAuthenticatedFetch.mockReset();
  });

  it("maps the transcript goal to the shared goal shape", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse({
        available: true,
        goal: {
          condition: "All tests pass",
          status: "complete",
          detail: "12 passed",
          iterations: 3,
          tokens: 1200,
          duration_ms: 125_900,
          set_at: 10.5,
          updated_at: 20.9,
        },
      }),
    );

    await expect(getClaudeGoal("conv/1")).resolves.toEqual({
      available: true,
      goal: {
        objective: "All tests pass",
        status: "complete",
        statusDetail: "12 passed",
        iterations: 3,
        tokenBudget: null,
        tokensUsed: 1200,
        timeUsedSeconds: 125,
        createdAt: 10,
        updatedAt: 20,
      },
    });
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv%2F1/claude_goal",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it.each([
    [
      { available: true, goal: null },
      { available: true, goal: null },
    ],
    [{ available: false, goal: null }, { available: false }],
  ])("reads %j as %j", async (body, expected) => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse(body));

    await expect(getClaudeGoal("conv")).resolves.toEqual(expected);
  });

  it.each([
    { available: true, goal: { status: "active" } },
    { available: false, goal: { condition: "All tests pass", status: "active" } },
    { goal: null },
    {},
  ])("rejects the malformed body %j", async (body) => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse(body));

    await expect(getClaudeGoal("conv")).rejects.toMatchObject({
      status: 502,
      code: "claude_goal_failed",
    });
  });

  it("keeps the server's error message and code", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse(
        { error: { code: "invalid_request", message: "Not a Claude session" } },
        400,
      ),
    );

    const error = await getClaudeGoal("conv").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 400,
      code: "invalid_request",
      message: "Not a Claude session",
    });
  });

  it("reports a stalled read as a timeout", async () => {
    vi.useFakeTimers();
    try {
      mockAuthenticatedFetch.mockReturnValueOnce(new Promise<Response>(() => {}));
      const read = getClaudeGoal("conv").catch((caught: unknown) => caught);

      await vi.advanceTimersByTimeAsync(60_000);

      expect(await read).toMatchObject({ status: 504, code: "claude_goal_request_timeout" });
    } finally {
      vi.useRealTimers();
    }
  });
});
