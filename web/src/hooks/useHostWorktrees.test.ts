import { createElement, type ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useHostWorktrees } from "./useHostWorktrees";

const authenticatedFetchMock = vi.fn();
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (url: string) => authenticatedFetchMock(url),
}));

const worktrees = [{ path: "/repo", branch: "main", is_main: true, detached: false }];

function response(status: number, body: unknown) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
}

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

afterEach(() => authenticatedFetchMock.mockReset());

describe("useHostWorktrees", () => {
  it("returns worktrees from a successful response", async () => {
    authenticatedFetchMock.mockResolvedValue(response(200, { object: "list", data: worktrees }));
    const { result } = renderHook(() => useHostWorktrees("host/one", "/repo"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(worktrees);
    expect(authenticatedFetchMock).toHaveBeenCalledWith(
      "/v1/hosts/host%2Fone/worktrees?path=%2Frepo",
    );
  });

  it.each([
    { detail: "worktree listing failed: not a git repository: /plain" },
    { error: { message: "Not a Git Repo" } },
    "fatal: not a git repository",
  ])("returns an empty list only for an explicit not-a-repo 400: %j", async (body) => {
    authenticatedFetchMock.mockResolvedValue(response(400, body));
    const { result } = renderHook(() => useHostWorktrees("host", "/plain"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
  });

  it.each([
    { detail: "worktree listing failed: permission denied" },
    { error: { message: "git failed: index locked" } },
    "",
  ])("does not classify an ambiguous 400 as non-git: %j", async (body) => {
    authenticatedFetchMock.mockResolvedValue(response(400, body));
    const { result } = renderHook(() => useHostWorktrees("host", "/repo"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
    expect(result.current.error?.message).toBe("host worktrees fetch failed: HTTP 400");
  });

  it("keeps an unreadable 400 response unknown", async () => {
    const unreadable = response(400, "");
    vi.spyOn(unreadable, "text").mockRejectedValue(new Error("response stream failed"));
    authenticatedFetchMock.mockResolvedValue(unreadable);
    const { result } = renderHook(() => useHostWorktrees("host", "/repo"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
    expect(result.current.error?.message).toBe("host worktrees fetch failed: HTTP 400");
  });

  it.each([401, 404, 409, 500])("surfaces HTTP %s without claiming non-git", async (status) => {
    authenticatedFetchMock.mockResolvedValue(response(status, { detail: "host unavailable" }));
    const { result } = renderHook(() => useHostWorktrees("host", "/repo"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
    expect(result.current.error?.message).toBe(`host worktrees fetch failed: HTTP ${status}`);
  });

  it("preserves known git worktrees when a refresh fails", async () => {
    authenticatedFetchMock.mockResolvedValueOnce(
      response(200, { object: "list", data: worktrees }),
    );
    const { result } = renderHook(() => useHostWorktrees("host", "/repo"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    authenticatedFetchMock.mockResolvedValueOnce(
      response(400, { detail: "git failed: permission denied" }),
    );
    await act(async () => {
      await result.current.refetch();
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toEqual(worktrees);
  });

  it.each([
    [null, "/repo"],
    ["host", null],
    ["host", ""],
  ])("does not fetch without a host and path (%s, %s)", (hostId, path) => {
    const { result } = renderHook(() => useHostWorktrees(hostId, path), { wrapper: wrapper() });
    expect(result.current.data).toBeUndefined();
    expect(authenticatedFetchMock).not.toHaveBeenCalled();
  });
});
