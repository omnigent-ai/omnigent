import { describe, expect, it } from "vitest";
import { __codexGoalApiErrorFromResponseForTest } from "./codexGoalApi";

function mockJsonResponse(body: unknown, init: { status: number; statusText?: string }): Response {
  return {
    ok: false,
    status: init.status,
    statusText: init.statusText ?? "Error",
    json: async () => body,
  } as unknown as Response;
}

describe("codex goal API errors", () => {
  it("reads standard nested Omnigent error envelopes", async () => {
    const err = await __codexGoalApiErrorFromResponseForTest(
      mockJsonResponse(
        { error: { code: "codex_native_goal_failed", message: "runner is asleep" } },
        { status: 503, statusText: "Service Unavailable" },
      ),
    );

    expect(err.status).toBe(503);
    expect(err.code).toBe("codex_native_goal_failed");
    expect(err.message).toBe("runner is asleep");
  });

  it("reads flat runner error envelopes preserved by the AP route", async () => {
    const err = await __codexGoalApiErrorFromResponseForTest(
      mockJsonResponse(
        { error: "invalid_input", detail: "harness mismatch" },
        { status: 400, statusText: "Bad Request" },
      ),
    );

    expect(err.status).toBe(400);
    expect(err.code).toBe("invalid_input");
    expect(err.message).toBe("harness mismatch");
  });
});
