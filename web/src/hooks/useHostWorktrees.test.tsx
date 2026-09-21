import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { useHostWorktrees, useVerifiedGithubWorktrees } from "./useHostWorktrees";

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
    expect(result.current.data?.[0].remote_provider).toBeUndefined();
  });

  it("does not reuse a prior path's GitHub identity while the next path loads", async () => {
    let resolveSecond: ((value: Response) => void) | undefined;
    authenticatedFetchMock
      .mockResolvedValueOnce(
        response([
          {
            path: "/github",
            branch: "main",
            is_main: true,
            detached: false,
            remote_provider: "github",
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

    await waitFor(() => expect(result.current.data?.[0].remote_provider).toBe("github"));
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
            remote_provider: "other",
          },
        ]),
      );
    });
    await waitFor(() => expect(result.current.data?.[0].remote_provider).toBe("other"));
  });
});

describe("useVerifiedGithubWorktrees", () => {
  const githubWorktrees = [
    {
      path: "/repo",
      branch: "main",
      is_main: true,
      detached: false,
      remote_provider: "github" as const,
    },
    {
      path: "/repo-worktrees/feature-x",
      branch: "feature/x",
      is_main: false,
      detached: false,
      remote_provider: "github" as const,
    },
  ];

  it("keeps verified GitHub worktrees visible while a nested path resolves", () => {
    const { result, rerender } = renderHook(
      ({ path, worktrees, resolved }) =>
        useVerifiedGithubWorktrees({
          hostId: "host_1",
          requestedPath: path,
          worktrees,
          resolved,
        }),
      {
        initialProps: { path: "/repo", worktrees: githubWorktrees, resolved: true },
      },
    );

    expect(result.current).toEqual(githubWorktrees);
    rerender({ path: "/repo/src/components", worktrees: undefined, resolved: false });
    expect(result.current).toEqual(githubWorktrees);
  });

  it("hides cached worktrees outside the verified roots and on explicit non-GitHub results", () => {
    const { result, rerender } = renderHook(
      ({ path, worktrees, resolved }) =>
        useVerifiedGithubWorktrees({
          hostId: "host_1",
          requestedPath: path,
          worktrees,
          resolved,
        }),
      {
        initialProps: { path: "/repo", worktrees: githubWorktrees, resolved: true },
      },
    );

    rerender({ path: "/ordinary", worktrees: undefined, resolved: false });
    expect(result.current).toEqual([]);
    rerender({
      path: "/repo/src",
      worktrees: [{ ...githubWorktrees[0], remote_provider: "other" as const }],
      resolved: true,
    });
    expect(result.current).toEqual([]);
  });
});
