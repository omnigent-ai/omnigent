// Tests for the sidebar preference sync hook + its API client.
//
// The hook fetches the user's server-stored sidebar preferences and exposes a
// write-through. It is what lets pins (and collapse/expand state) survive a
// fresh browser and follow the user across devices, with localStorage demoted
// to a cache. Fetch is mocked so no server is required.

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getSidebarPreferences, putSidebarPreference } from "@/lib/sidebarPreferencesApi";
import { useSidebarPreferences } from "./useSidebarPreferences";

function mockFetchOnce(body: unknown, ok = true, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    json: () => Promise.resolve(body),
  } as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("sidebarPreferencesApi", () => {
  it("getSidebarPreferences keeps only well-formed string-array values", async () => {
    mockFetchOnce({
      object: "preferences",
      preferences: {
        pinned_conversation_ids: ["a", "b"],
        collapsed_sidebar_sections: ["Chats"],
        // Malformed values are dropped, left to the local cache.
        expanded_project_sections: "not-an-array",
        unknown_key: ["x"],
      },
    });
    const prefs = await getSidebarPreferences();
    expect(prefs).toEqual({
      pinned_conversation_ids: ["a", "b"],
      collapsed_sidebar_sections: ["Chats"],
    });
  });

  it("getSidebarPreferences rejects on a non-OK response", async () => {
    mockFetchOnce({}, false, 401);
    await expect(getSidebarPreferences()).rejects.toThrow();
  });

  it("putSidebarPreference PUTs the value to the keyed endpoint", async () => {
    const fetchMock = mockFetchOnce({ object: "preference" });
    await putSidebarPreference("pinned_conversation_ids", ["a", "b"]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/preferences/pinned_conversation_ids");
    expect(init).toMatchObject({ method: "PUT" });
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ value: ["a", "b"] });
  });
});

describe("useSidebarPreferences", () => {
  it("does not fetch when disabled", async () => {
    const fetchMock = mockFetchOnce({ object: "preferences", preferences: {} });
    const { result } = renderHook(() => useSidebarPreferences(false), { wrapper: wrapper() });
    // Give react-query a tick; a disabled query must never fire.
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.isResolved).toBe(false);
    expect(result.current.serverPreferences).toBeUndefined();
  });

  it("resolves the server preferences when enabled", async () => {
    mockFetchOnce({
      object: "preferences",
      preferences: { pinned_conversation_ids: ["server-pin"] },
    });
    const { result } = renderHook(() => useSidebarPreferences(true), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isResolved).toBe(true));
    expect(result.current.serverPreferences).toEqual({
      pinned_conversation_ids: ["server-pin"],
    });
  });

  it("stays unresolved when the fetch fails, so the sidebar stays local", async () => {
    mockFetchOnce({}, false, 401);
    const { result } = renderHook(() => useSidebarPreferences(true), { wrapper: wrapper() });
    // Query settles into an error state; never reports resolved.
    await waitFor(() => expect(result.current.isResolved).toBe(false));
    expect(result.current.serverPreferences).toBeUndefined();
  });

  it("writePreference PUTs and updates the shared cache", async () => {
    const fetchMock = mockFetchOnce({ object: "preferences", preferences: {} });
    const { result } = renderHook(() => useSidebarPreferences(true), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isResolved).toBe(true));

    result.current.writePreference("pinned_conversation_ids", ["new-pin"]);

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(
        ([url]) => url === "/v1/preferences/pinned_conversation_ids",
      );
      expect(putCall).toBeDefined();
    });
    // Optimistic cache update means the resolved value reflects the write.
    await waitFor(() =>
      expect(result.current.serverPreferences?.pinned_conversation_ids).toEqual(["new-pin"]),
    );
  });

  it("writePreference is a no-op when disabled", async () => {
    const fetchMock = mockFetchOnce({ object: "preferences", preferences: {} });
    const { result } = renderHook(() => useSidebarPreferences(false), { wrapper: wrapper() });
    result.current.writePreference("pinned_conversation_ids", ["x"]);
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
