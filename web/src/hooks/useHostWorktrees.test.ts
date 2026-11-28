import { createElement, type ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type HostWorktree,
  isAbsoluteHostPath,
  isWorktreeRoot,
  matchWorktree,
  pathInWorktree,
  trimHostPath,
  useHostWorktrees,
} from "./useHostWorktrees";

const authenticatedFetchMock = vi.fn();
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (url: string) => authenticatedFetchMock(url),
}));

const worktrees = [{ path: "/repo", branch: "main", is_main: true, detached: false }];

function response(status: number, body: unknown) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
}

function wrapper() {
  // The hook sets its own `retry`, so only the delay matters here: keep retries
  // instant so the retrying error cases do not wait on exponential backoff.
  const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } });
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

  it("does not retry a 400 the host already answered", async () => {
    authenticatedFetchMock.mockResolvedValue(
      response(400, { detail: "worktree listing failed: git command timed out after 120s" }),
    );
    const { result } = renderHook(() => useHostWorktrees("host", "/repo"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(authenticatedFetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.error?.message).toBe("host worktrees fetch failed: HTTP 400");
  });

  it("keeps retrying when the host could not be reached", async () => {
    authenticatedFetchMock.mockResolvedValue(response(409, { detail: "host did not respond" }));
    const { result } = renderHook(() => useHostWorktrees("host", "/repo"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(authenticatedFetchMock.mock.calls.length).toBeGreaterThan(1);
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

describe("isWorktreeRoot", () => {
  it.each([
    ["the same POSIX path", "/repo", "/repo", true],
    ["a trailing slash", "/repo/", "/repo", true],
    ["a folder inside it", "/repo", "/repo/src", false],
    ["POSIX case", "/Repo", "/repo", false],
    ["Windows separators", "C:/repo", "C:\\repo", true],
    ["a Windows drive in another case", "C:\\repo", "c:\\repo", true],
    ["a Windows folder inside it", "C:/repo", "C:\\repo\\app", false],
  ])("compares %s", (_case, root, path, same) => {
    expect(isWorktreeRoot(root, path)).toBe(same);
  });
});

describe("pathInWorktree", () => {
  it.each([
    ["the root itself", "/repo", "/repo/", ""],
    ["a folder inside it", "/repo", "/repo/src/", "src"],
    ["a sibling sharing the name's start", "/repo", "/repository", null],
    ["a folder of the filesystem root", "/", "/repo", "repo"],
    ["a POSIX name holding a backslash", "/repo", "/repo/a\\b", "a\\b"],
    ["a POSIX sibling whose name holds a backslash", "/repo", "/repo\\other/src", null],
    ["POSIX case", "/Repo", "/repo/src", null],
    ["a folder of a Windows drive's root", "C:\\", "c:/repo", "repo"],
    ["a Windows folder with mixed separators", "c:/repo", "C:\\repo\\app\\", "app"],
    ["a Windows folder in another case, in its own spelling", "C:/repo", "c:\\REPO\\Src", "Src"],
  ])("places %s", (_case, root, path, relative) => {
    expect(pathInWorktree(root, path)).toBe(relative);
  });
});

describe("isAbsoluteHostPath and trimHostPath", () => {
  it.each([
    ["/repo", true],
    ["C:\\repo", true],
    ["c:/repo", true],
    ["\\\\server\\share", true],
    ["//server/share", true],
    ["src/a.ts", false],
    ["C:repo", false],
  ])("reads %s as absolute: %s", (path, absolute) => {
    expect(isAbsoluteHostPath(path)).toBe(absolute);
  });

  it.each([
    ["/repo//", "/repo"],
    ["/", "/"],
    ["/repo/worktree\\", "/repo/worktree\\"],
    ["C:\\repo\\", "C:\\repo"],
    ["C:\\", "C:\\"],
    ["C:/", "C:/"],
    ["C:\\\\", "C:\\"],
    ["\\\\server\\share\\", "\\\\server\\share"],
  ])("trims %s to %s", (path, trimmed) => {
    expect(trimHostPath(path)).toBe(trimmed);
  });
});

describe("matchWorktree", () => {
  const share = (path: string): HostWorktree => ({
    path,
    branch: "main",
    is_main: true,
    detached: false,
  });

  it.each([
    ["a UNC share across separators", "//server/share/repo", "\\\\server\\share\\repo", true],
    ["a UNC share in another case", "\\\\Server\\Share\\Repo", "\\\\server\\share\\repo", true],
    [
      "a folder inside a UNC share",
      "\\\\server\\share\\repo",
      "//server/share/repo/packages/web",
      true,
    ],
    [
      "a forward-slash UNC share in another case",
      "//Server/Share/repo",
      "//server/share/Repo",
      true,
    ],
    [
      "a forward-slash UNC folder in another case",
      "//server/share/Repo",
      "//Server/Share/repo/src",
      true,
    ],
    [
      "another share on the same server",
      "\\\\server\\share\\repo",
      "\\\\server\\other\\repo",
      false,
    ],
    ["a POSIX path with a backslash, unchanged", "/home/a/repo", "/home/a/repo\\evil", false],
  ])("matches %s: %s", (_case, root, workspace, matches) => {
    const worktree = share(root);
    expect(matchWorktree([worktree], workspace)).toBe(matches ? worktree : null);
  });
});
