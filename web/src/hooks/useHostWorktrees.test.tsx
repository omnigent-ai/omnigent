import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { type HostWorktree, useHostWorktrees, useVerifiedGitWorktrees } from "./useHostWorktrees";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const authenticatedFetchMock = vi.mocked(authenticatedFetch);

function response(data: object[]): Response {
  return new Response(JSON.stringify({ object: "list", data }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function wrapper(client: QueryClient) {
  return function QueryWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useHostWorktrees", () => {
  beforeEach(() => {
    authenticatedFetchMock.mockReset();
  });

  it("accepts an old-host response without remote_provider", async () => {
    authenticatedFetchMock.mockResolvedValue(
      response([{ path: "/repo", branch: "main", is_main: true, detached: false }]),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useHostWorktrees("host_1", "/repo"), {
      wrapper: wrapper(client),
    });

    await waitFor(() => expect(result.current.data).toHaveLength(1));
    expect(result.current.data?.[0]).not.toHaveProperty("remote_provider");
  });

  it.each([
    { hostId: "host_1", path: "/ordinary" },
    { hostId: "host_2", path: "/github" },
  ])("ignores stale worktrees when switching to $hostId:$path", async (next) => {
    let resolveStale!: (value: Response) => void;
    let resolveNext!: (value: Response) => void;
    const worktree = (branch: string) =>
      response([{ path: "/github", branch, is_main: true, detached: false }]);
    authenticatedFetchMock
      .mockResolvedValueOnce(worktree("original"))
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveStale = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveNext = resolve;
          }),
      );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result, rerender } = renderHook(({ hostId, path }) => useHostWorktrees(hostId, path), {
      initialProps: { hostId: "host_1", path: "/github" },
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.data?.[0].branch).toBe("original"));
    act(() => {
      void result.current.refetch();
    });
    await waitFor(() => expect(resolveStale).toBeDefined());
    rerender(next);
    expect(result.current.data).toBeUndefined();
    expect(result.current.isPlaceholderData).toBe(false);
    await waitFor(() => expect(resolveNext).toBeDefined());
    await act(async () => {
      resolveNext(worktree("selected"));
    });
    await waitFor(() => expect(result.current.data?.[0].branch).toBe("selected"));
    await act(async () => {
      resolveStale(worktree("stale"));
    });
    expect(result.current.data?.[0].branch).toBe("selected");
  });
});

describe("useVerifiedGitWorktrees", () => {
  it.each([undefined, null, "other", "github"] as const)(
    "recognizes Git worktrees regardless of provider metadata (%s)",
    (remoteProvider) => {
      const worktrees: HostWorktree[] = [
        { path: "/repo", branch: "main", is_main: true, detached: false },
        { path: "/repo-worktrees/feature-x", branch: "feature/x", is_main: false, detached: false },
      ].map((worktree) => ({
        ...worktree,
        ...(remoteProvider === undefined ? {} : { remote_provider: remoteProvider }),
      }));
      interface Props {
        hostId: string;
        requestedPath: string;
        worktrees: HostWorktree[] | undefined;
        resolved: boolean;
      }
      const { result, rerender } = renderHook((props: Props) => useVerifiedGitWorktrees(props), {
        initialProps: { hostId: "host_1", requestedPath: "/repo", worktrees, resolved: true },
      });

      expect(result.current).toEqual(worktrees);
      const pending = { hostId: "host_1", worktrees: undefined, resolved: false };
      rerender({ ...pending, requestedPath: "/repo/src/components" });
      expect(result.current).toEqual(worktrees);
      rerender({ ...pending, requestedPath: "/repo-worktrees/feature-x/src" });
      expect(result.current).toEqual(worktrees);

      rerender({ ...pending, requestedPath: "/repo-other" });
      expect(result.current).toEqual([]);
      rerender({ ...pending, hostId: "host_2", requestedPath: "/repo" });
      expect(result.current).toEqual([]);

      rerender({ ...pending, requestedPath: "/repo/src", worktrees: [], resolved: true });
      expect(result.current).toEqual([]);
      rerender({ ...pending, requestedPath: "/repo/src" });
      expect(result.current).toEqual([]);
    },
  );
});
