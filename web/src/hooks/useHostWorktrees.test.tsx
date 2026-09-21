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

  it("does not reuse a prior path's worktrees while the next path loads", async () => {
    let resolveSecond: ((value: Response) => void) | undefined;
    authenticatedFetchMock
      .mockResolvedValueOnce(
        response([
          {
            path: "/github",
            branch: "main",
            is_main: true,
            detached: false,
          },
        ]),
      )
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveSecond = resolve;
          }),
      );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result, rerender } = renderHook(
      ({ path }: { path: string }) => useHostWorktrees("host_1", path),
      { initialProps: { path: "/github" }, wrapper: wrapper(client) },
    );

    await waitFor(() => expect(result.current.data?.[0].path).toBe("/github"));
    rerender({ path: "/ordinary" });
    expect(result.current.data).toBeUndefined();
    expect(result.current.isPlaceholderData).toBe(false);

    await act(async () => {
      resolveSecond?.(
        response([
          {
            path: "/ordinary",
            branch: "main",
            is_main: true,
            detached: false,
          },
        ]),
      );
    });
    await waitFor(() => expect(result.current.data?.[0].path).toBe("/ordinary"));
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
