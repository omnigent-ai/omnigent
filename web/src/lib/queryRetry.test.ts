import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./sessionsApi";
import {
  isRetryableRequestError,
  QUERY_RETRY_AFTER_MAX_MS,
  QUERY_RETRY_BASE_MS,
  QUERY_RETRY_MAX_MS,
  queryRetryDelay,
} from "./queryRetry";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("isRetryableRequestError", () => {
  it("retries server errors and throttling, not client errors the same request repeats", () => {
    for (const status of [500, 502, 503, 504, 408, 425, 429]) {
      expect(isRetryableRequestError(new ApiError("x", status, null))).toBe(true);
    }
    for (const status of [400, 401, 403, 404, 409, 422]) {
      expect(isRetryableRequestError(new ApiError("x", status, null))).toBe(false);
    }
  });

  it("retries wrong_replica, which clears once routing settles", () => {
    expect(isRetryableRequestError(new ApiError("moved", 400, "wrong_replica"))).toBe(true);
    expect(isRetryableRequestError(new ApiError("bad", 400, "invalid_input"))).toBe(false);
  });

  it("keeps retrying network failures and errors without a status", () => {
    expect(isRetryableRequestError(new TypeError("Failed to fetch"))).toBe(true);
    expect(isRetryableRequestError(new Error("500 Internal Server Error"))).toBe(true);
  });
});

describe("queryRetryDelay", () => {
  it("doubles from the base and jitters to 50–150% of it", () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    expect(queryRetryDelay(0, new Error("x"))).toBe(QUERY_RETRY_BASE_MS / 2);
    expect(queryRetryDelay(2, new Error("x"))).toBe((QUERY_RETRY_BASE_MS * 4) / 2);
    vi.spyOn(Math, "random").mockReturnValue(1);
    expect(queryRetryDelay(0, new Error("x"))).toBe(QUERY_RETRY_BASE_MS * 1.5);
  });

  it("caps the base before jittering it", () => {
    const random = vi.spyOn(Math, "random").mockReturnValue(0);
    expect(queryRetryDelay(20, new Error("x"))).toBe(QUERY_RETRY_MAX_MS / 2);
    random.mockReturnValue(1);
    expect(queryRetryDelay(20, new Error("x"))).toBe(QUERY_RETRY_MAX_MS * 1.5);
  });

  it("waits the server's Retry-After, bounded", () => {
    expect(queryRetryDelay(0, new ApiError("slow down", 429, null, 12_000))).toBe(12_000);
    expect(queryRetryDelay(0, new ApiError("slow down", 429, null, 10 * 60_000))).toBe(
      QUERY_RETRY_AFTER_MAX_MS,
    );
  });
});
