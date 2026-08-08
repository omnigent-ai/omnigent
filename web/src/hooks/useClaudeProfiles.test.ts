// Unit tests for the Claude-account profile query hook: it unwraps the
// {object, data} envelope, and it never rejects — the picker hides itself
// on an empty list, so a non-OK reply, a body that is not JSON, or a body
// that is not the envelope shape must all resolve to [].

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useClaudeProfiles } from "./useClaudeProfiles";

function mockResponse(
  json: () => Promise<unknown>,
  init?: { ok?: boolean; status?: number },
): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json,
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

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

describe("useClaudeProfiles", () => {
  it("unwraps the envelope into name/display pairs", async () => {
    fetchMock.mockResolvedValue(
      mockResponse(async () => ({
        object: "list",
        data: [
          { name: "work", display: "Work (Anthropic)" },
          { name: "personal", display: "personal" },
        ],
      })),
    );

    const { result } = renderHook(() => useClaudeProfiles(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([
      { name: "work", display: "Work (Anthropic)" },
      { name: "personal", display: "personal" },
    ]);
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/claude-profiles");
  });

  it("resolves to an empty list on a non-OK reply", async () => {
    fetchMock.mockResolvedValue(
      mockResponse(async () => ({ detail: "boom" }), { ok: false, status: 500 }),
    );

    const { result } = renderHook(() => useClaudeProfiles(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
  });

  it("resolves to an empty list when the body is not JSON", async () => {
    // A proxy or error page answering 200 with HTML: res.json() rejects.
    fetchMock.mockResolvedValue(
      mockResponse(async () => {
        throw new SyntaxError("Unexpected token < in JSON at position 0");
      }),
    );

    const { result } = renderHook(() => useClaudeProfiles(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
  });

  it("resolves to an empty list when the body is not the envelope shape", async () => {
    // Valid JSON, wrong shape — a bare null and a non-array `data` both
    // reach `.map` in the naive version.
    fetchMock.mockResolvedValueOnce(mockResponse(async () => null));

    const first = renderHook(() => useClaudeProfiles(), { wrapper });
    await waitFor(() => expect(first.result.current.isSuccess).toBe(true));
    expect(first.result.current.data).toEqual([]);

    fetchMock.mockResolvedValueOnce(mockResponse(async () => ({ data: "nope" })));

    const second = renderHook(() => useClaudeProfiles(), { wrapper });
    await waitFor(() => expect(second.result.current.isSuccess).toBe(true));
    expect(second.result.current.data).toEqual([]);
  });
});
