// Drives `useViewerId` against the real identity module: a first `GET /v1/me`
// that fails in transit must not leave the viewer unknown for the page's
// lifetime, since every ownership-gated row action reads this id.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.resetModules();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("recovers the viewer id after the first probe is dropped", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify({ user_id: "alice@example.com" }), { status: 200 }),
  );
  const { useViewerId } = await import("./useViewerId");

  const { result } = renderHook(useViewerId);
  await act(async () => {});
  expect(result.current).toBeNull();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_000);
  });
  expect(result.current).toBe("alice@example.com");
  expect(fetchMock).toHaveBeenCalledTimes(2);
});
