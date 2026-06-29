// Unit tests for the work-items hooks: the request URL/method/body contract,
// the {object,data} envelope unwrap, throw-on-non-2xx, and that mutations
// invalidate the shared ["work-items"] key so the Tasks list refreshes.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDeleteWorkItem, useUpdateWorkItem, useWorkItems } from "./useWorkItems";
import type { WorkItem } from "@/lib/workItemsApi";

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

function makeItem(overrides: Partial<WorkItem> & { id: string }): WorkItem {
  return {
    object: "work_item",
    source: "manual",
    external_id: null,
    dedup_key: `manual:${overrides.id}`,
    title: "t",
    body: null,
    status: "new",
    pr_url: null,
    conversation_id: null,
    assignee_user_id: null,
    created_by: null,
    plan: null,
    created_at: 0,
    updated_at: null,
    ...overrides,
  };
}

describe("useWorkItems", () => {
  it("GETs /v1/work-items and unwraps the data array", async () => {
    const items = [makeItem({ id: "wi1" }), makeItem({ id: "wi2" })];
    fetchMock.mockResolvedValue(mockResponse({ object: "list", data: items }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useWorkItems(), { wrapper: wrapperWith(queryClient) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/work-items");
    expect(result.current.data).toEqual(items);
  });

  it("passes a status filter as a query param", async () => {
    fetchMock.mockResolvedValue(mockResponse({ object: "list", data: [] }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useWorkItems({ status: "needs_review" }), {
      wrapper: wrapperWith(queryClient),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/work-items?status=needs_review");
  });

  it("surfaces an error on non-2xx", async () => {
    fetchMock.mockResolvedValue(mockResponse({}, { ok: false, status: 500 }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useWorkItems(), { wrapper: wrapperWith(queryClient) });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("useUpdateWorkItem", () => {
  it("PATCHes the encoded id with the patch and invalidates the list", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(makeItem({ id: "wi 1", status: "done" })));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useUpdateWorkItem(), { wrapper: wrapperWith(queryClient) });
    result.current.mutate({ id: "wi 1", patch: { status: "done" } });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/work-items/wi%201");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ status: "done" });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["work-items"] });
  });
});

describe("useDeleteWorkItem", () => {
  it("DELETEs the encoded id and invalidates the list", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useDeleteWorkItem(), { wrapper: wrapperWith(queryClient) });
    result.current.mutate("wi9");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/work-items/wi9");
    expect(init.method).toBe("DELETE");
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["work-items"] });
  });
});
