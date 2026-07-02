// Unit tests for the schedules hooks: conversation-scoped list URL, the
// {object,data} unwrap, PATCH/DELETE contracts, and list invalidation.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDeleteSchedule, useSchedules, useUpdateSchedule } from "./useSchedules";

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

function wrapperWith(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

describe("useSchedules", () => {
  it("GETs the conversation-scoped list and unwraps data", async () => {
    fetchMock.mockResolvedValue(mockResponse({ object: "list", data: [{ id: "sch_1" }] }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useSchedules("conv_1"), { wrapper: wrapperWith(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/schedules?conversation_id=conv_1");
    expect(result.current.data).toEqual([{ id: "sch_1" }]);
  });

  it("is disabled without a conversation id", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useSchedules(undefined), { wrapper: wrapperWith(qc) });
    expect(result.current.fetchStatus).toBe("idle");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("useUpdateSchedule / useDeleteSchedule", () => {
  it("PATCHes enabled and invalidates", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "sch_1", enabled: false }));
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const spy = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => useUpdateSchedule(), { wrapper: wrapperWith(qc) });
    result.current.mutate({ id: "sch_1", patch: { enabled: false } });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/schedules/sch_1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ enabled: false });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["schedules"] });
  });

  it("DELETEs and invalidates", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const spy = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => useDeleteSchedule(), { wrapper: wrapperWith(qc) });
    result.current.mutate("sch_9");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/schedules/sch_9");
    expect(init.method).toBe("DELETE");
    expect(spy).toHaveBeenCalledWith({ queryKey: ["schedules"] });
  });
});
