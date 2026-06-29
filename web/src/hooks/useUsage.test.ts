// Unit tests for the usage hook: GET /v1/usage, returns the summary object
// as-is (no envelope), and throws on non-2xx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useUsage } from "./useUsage";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function wrapperWith(queryClient: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

describe("useUsage", () => {
  it("GETs /v1/usage and returns the summary", async () => {
    const summary = {
      object: "usage",
      conversations: 2,
      totals: {
        input_tokens: 300,
        output_tokens: 30,
        total_tokens: 330,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
        total_cost_usd: 1.5,
      },
      by_model: { "claude-sonnet-4-6": { input_tokens: 300 } },
    };
    fetchMock.mockResolvedValue(mockResponse(summary));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useUsage(), { wrapper: wrapperWith(queryClient) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/usage");
    expect(result.current.data).toEqual(summary);
  });

  it("surfaces an error on non-2xx", async () => {
    fetchMock.mockResolvedValue(mockResponse({}, { ok: false, status: 500 }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useUsage(), { wrapper: wrapperWith(queryClient) });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
