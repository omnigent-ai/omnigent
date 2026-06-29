// Unit tests for the canvas hook: GET /v1/canvas/{id}, returns the canvas,
// maps 404 → null (no canvas), throws on other non-2xx, and stays disabled
// without a conversation id.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useCanvas } from "./useCanvas";

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

describe("useCanvas", () => {
  it("GETs /v1/canvas/{id} and returns the canvas", async () => {
    const canvas = {
      id: "cnv_1",
      object: "canvas",
      conversation_id: "conv_1",
      title: "Report",
      content: "<h1>Hi</h1>",
      content_type: "html",
      created_at: 0,
      updated_at: null,
    };
    fetchMock.mockResolvedValue(mockResponse(canvas));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useCanvas("conv_1"), { wrapper: wrapperWith(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/canvas/conv_1");
    expect(result.current.data).toEqual(canvas);
  });

  it("maps 404 to null (no canvas set)", async () => {
    fetchMock.mockResolvedValue(mockResponse({}, { ok: false, status: 404 }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useCanvas("conv_1"), { wrapper: wrapperWith(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
  });

  it("throws on a non-404 error", async () => {
    fetchMock.mockResolvedValue(mockResponse({}, { ok: false, status: 500 }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useCanvas("conv_1"), { wrapper: wrapperWith(qc) });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });

  it("is disabled without a conversation id", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useCanvas(undefined), { wrapper: wrapperWith(qc) });
    expect(result.current.fetchStatus).toBe("idle");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
