import { createElement, type ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSessionWorktrees } from "./useSessionWorktrees";

const authenticatedFetchMock = vi.fn();
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (url: string) => authenticatedFetchMock(url),
}));

function res(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  };
}

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

afterEach(() => authenticatedFetchMock.mockReset());

describe("useSessionWorktrees", () => {
  it("returns the parsed worktrees on 200", async () => {
    authenticatedFetchMock.mockResolvedValue(
      res(200, {
        object: "list",
        data: [{ path: "/w", branch: "main", is_main: true, detached: false }],
      }),
    );
    const { result } = renderHook(() => useSessionWorktrees("c", "h", "/w"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toEqual({
      status: "ok",
      worktrees: [{ path: "/w", branch: "main", is_main: true, detached: false }],
    });
  });

  it("classifies a not-a-git-repo 400 as not_git", async () => {
    authenticatedFetchMock.mockResolvedValue(res(400, { detail: "fatal: not a git repository" }));
    const { result } = renderHook(() => useSessionWorktrees("c", "h", "/w"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toEqual({ status: "not_git" });
  });

  it("classifies any other 400 git failure as unknown", async () => {
    authenticatedFetchMock.mockResolvedValue(res(400, { detail: "git failed: index locked" }));
    const { result } = renderHook(() => useSessionWorktrees("c", "h", "/w"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toEqual({ status: "unknown" });
  });

  it("treats an offline host (409) as unknown, not not_git", async () => {
    authenticatedFetchMock.mockResolvedValue(res(409, { detail: "host offline" }));
    const { result } = renderHook(() => useSessionWorktrees("c", "h", "/w"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toEqual({ status: "unknown" });
  });

  it("does not carry a prior workspace's result across a host switch", async () => {
    // hostA on /w → branchA; hostB on the SAME path → branchB. Without
    // placeholderData, the switch must go through an undefined (loading) state
    // rather than surfacing hostA's worktrees under hostB.
    authenticatedFetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        res(200, {
          object: "list",
          data: [
            {
              path: "/w",
              branch: url.includes("hostA") ? "branchA" : "branchB",
              is_main: true,
              detached: false,
            },
          ],
        }),
      ),
    );
    const { result, rerender } = renderHook(
      ({ hostId }: { hostId: string }) => useSessionWorktrees("c", hostId, "/w"),
      { wrapper: wrapper(), initialProps: { hostId: "hostA" } },
    );
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toMatchObject({ worktrees: [{ branch: "branchA" }] });

    rerender({ hostId: "hostB" });
    // Immediately after the switch: no stale hostA data advertised as hostB's.
    expect(result.current.data).toBeUndefined();
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toMatchObject({ worktrees: [{ branch: "branchB" }] });
  });

  it("is disabled without a host or workspace", () => {
    const { result } = renderHook(() => useSessionWorktrees("c", null, "/w"), {
      wrapper: wrapper(),
    });
    expect(result.current.data).toBeUndefined();
    expect(authenticatedFetchMock).not.toHaveBeenCalled();
  });
});
