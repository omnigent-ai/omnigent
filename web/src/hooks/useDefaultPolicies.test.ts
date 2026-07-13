// Unit tests for the default-policies admin CRUD hooks: the request
// URL/method/body contract of each operation, the throw-on-non-2xx guard,
// and that every mutation invalidates the shared ["default-policies"] key
// so the admin list reflects the change without a reload.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useAddDefaultPolicy,
  useDefaultPolicies,
  useDeleteDefaultPolicy,
  useUpdateDefaultPolicy,
  type DefaultPolicy,
} from "./useDefaultPolicies";

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

function makePolicy(overrides: Partial<DefaultPolicy> & { id: string }): DefaultPolicy {
  return {
    object: "default_policy",
    name: "p",
    type: "python",
    handler: "h",
    factory_params: null,
    enabled: true,
    created_at: 0,
    updated_at: null,
    created_by: null,
    ...overrides,
  };
}

describe("useDefaultPolicies", () => {
  it("GETs /v1/policies and unwraps the data array from the envelope", async () => {
    // The endpoint wraps rows in {object, data}; the hook must return the
    // inner array, otherwise consumers get an object where they expect a list.
    const policies = [makePolicy({ id: "p1" }), makePolicy({ id: "p2" })];
    fetchMock.mockResolvedValueOnce(mockResponse({ object: "list", data: policies }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useDefaultPolicies(), {
      wrapper: wrapperWith(queryClient),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/policies");
    expect(result.current.data).toEqual(policies);
  });

  it("surfaces an error on non-2xx", async () => {
    // A failed list fetch must error, not cache an empty list that hides
    // policies from the admin.
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 500 }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useDefaultPolicies(), {
      wrapper: wrapperWith(queryClient),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({ message: expect.stringContaining("500") });
  });
});

describe("useAddDefaultPolicy", () => {
  function renderAdd(queryClient: QueryClient) {
    return renderHook(() => useAddDefaultPolicy(), { wrapper: wrapperWith(queryClient) });
  }

  it("POSTs the payload to /v1/policies and invalidates the list", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(makePolicy({ id: "p1" })));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderAdd(queryClient);
    result.current.mutate({ name: "p", type: "python", handler: "h" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/policies");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ name: "p", type: "python", handler: "h" });
    // The new policy must show up in the list without a reload.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["default-policies"] });
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 422 }));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderAdd(queryClient);
    result.current.mutate({ name: "p", type: "url", handler: "https://x" });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("useUpdateDefaultPolicy", () => {
  function renderUpdate(queryClient: QueryClient) {
    return renderHook(() => useUpdateDefaultPolicy(), { wrapper: wrapperWith(queryClient) });
  }

  it("PATCHes the encoded policy id with the enabled flag and invalidates", async () => {
    // The id is url-encoded so ids with reserved chars hit the right route;
    // only the enabled flag is sent (the toggle is the only mutable field here).
    fetchMock.mockResolvedValueOnce(mockResponse(makePolicy({ id: "p 1", enabled: false })));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderUpdate(queryClient);
    result.current.mutate({ policyId: "p 1", enabled: false });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/policies/p%201");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ enabled: false });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["default-policies"] });
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 404 }));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderUpdate(queryClient);
    result.current.mutate({ policyId: "p1", enabled: true });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("useDeleteDefaultPolicy", () => {
  function renderDelete(queryClient: QueryClient) {
    return renderHook(() => useDeleteDefaultPolicy(), { wrapper: wrapperWith(queryClient) });
  }

  it("DELETEs the encoded policy id and invalidates the list", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(undefined));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderDelete(queryClient);
    result.current.mutate("p 1");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/policies/p%201");
    expect(init.method).toBe("DELETE");
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["default-policies"] });
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 403 }));
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderDelete(queryClient);
    result.current.mutate("p1");
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
