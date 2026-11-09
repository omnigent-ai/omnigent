import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./sessionsApi";
import { APP_QUERY_DEFAULTS, isRetryableRequestError, queryRetryDelay } from "./queryRetry";

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
  it("doubles from 1 s and jitters to 50–150% of it", () => {
    const random = vi.spyOn(Math, "random").mockReturnValue(0.5);
    expect([0, 1, 2].map((n) => queryRetryDelay(n, new Error("x")))).toEqual([1_000, 2_000, 4_000]);
    random.mockReturnValue(0);
    expect(queryRetryDelay(0, new Error("x"))).toBe(500);
    random.mockReturnValue(1);
    expect(queryRetryDelay(0, new Error("x"))).toBe(1_500);
  });

  it("caps the base at 30 s before jittering it", () => {
    const random = vi.spyOn(Math, "random").mockReturnValue(0.5);
    expect(queryRetryDelay(20, new Error("x"))).toBe(30_000);
    random.mockReturnValue(0);
    expect(queryRetryDelay(20, new Error("x"))).toBe(15_000);
    random.mockReturnValue(1);
    expect(queryRetryDelay(20, new Error("x"))).toBe(45_000);
  });

  it("waits the server's Retry-After, bounded, stretched by up to a quarter", () => {
    const random = vi.spyOn(Math, "random").mockReturnValue(0);
    expect(queryRetryDelay(0, new ApiError("slow down", 429, null, 12_000))).toBe(12_000);
    expect(queryRetryDelay(0, new ApiError("slow down", 429, null, 10 * 60_000))).toBe(60_000);
    random.mockReturnValue(1);
    expect(queryRetryDelay(0, new ApiError("slow down", 429, null, 12_000))).toBe(15_000);
    // An oversized hint is capped at 60 s, then stretched to at most 75 s.
    expect(queryRetryDelay(0, new ApiError("slow down", 429, null, 10 * 60_000))).toBe(75_000);
    // A zero hint means "now"; an absent one falls back to the jittered backoff.
    expect(queryRetryDelay(0, new ApiError("now", 503, null, 0))).toBe(0);
    random.mockReturnValue(0);
    expect(queryRetryDelay(0, new ApiError("plain", 503, null))).toBe(500);
  });
});

describe("APP_QUERY_DEFAULTS", () => {
  it("paces retries for every query in both app roots, without changing retry counts", () => {
    expect(APP_QUERY_DEFAULTS.retryDelay).toBe(queryRetryDelay);
    expect(APP_QUERY_DEFAULTS).not.toHaveProperty("retry");
  });
});
