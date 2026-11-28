// Commits read by the session's host: ListPullRequestCommits at the shown head, shaped as the
// page's list, and GetCommitDiff for one commit; neither touches the fast track.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ read: vi.fn(), fastTrack: vi.fn() }));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: host.read,
}));
vi.mock("@/lib/githubFastTrack", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  queryGitHubFastTrack: host.fastTrack,
}));

import {
  type PullRequestCommitsSource,
  pullRequestCommitsQueryKey,
  useCommitDiff,
  usePullRequestCommits,
} from "@/hooks/usePullRequestCommits";
import type { WorkspaceClient } from "@/lib/workspaceApi";

const PR = "https://github.com/acme/app/pull/7";
const HEAD = "a".repeat(40);
const BASE = "b".repeat(40);

const source = (conversationId = "s1"): PullRequestCommitsSource => ({
  account: { host: "github.com", login: "" },
  repository: "acme/app",
  number: 7,
  headSha: HEAD,
  baseSha: BASE,
  host: { conversationId, prUrl: PR },
});

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

/** The workspace call the reader made, run against a client that records its request. */
async function sentRequest(call: number): Promise<{ method: string; request: unknown }> {
  const [, method, read] = host.read.mock.calls[call]!;
  let request: unknown;
  const record = (input: unknown) => {
    request = input;
    return Promise.resolve({});
  };
  await read({
    listPullRequestCommits: record,
    getCommitDiff: record,
  } as unknown as WorkspaceClient);
  return { method, request };
}

beforeEach(() => {
  host.read.mockReset();
  host.fastTrack.mockReset();
});

describe("usePullRequestCommits on the session's host", () => {
  it("lists the commits at the shown head, in the page list’s shape", async () => {
    host.read.mockResolvedValue({
      object: "list",
      has_more: false,
      total_count: 2,
      head_sha: HEAD,
      base_sha: BASE,
      data: [
        {
          sha: "c".repeat(40),
          title: "Strip input",
          body: "Why it strips",
          author: "Octo",
          author_login: "octo",
          date: "2026-11-20T09:00:00Z",
          committed_at: "2026-11-20T09:05:00Z",
          url: "https://github.com/acme/app/commit/c",
        },
        { sha: HEAD, title: "Describe it", author: "Octo", date: "2026-11-20T10:00:00Z" },
      ],
    });
    const { result } = renderHook(() => usePullRequestCommits(source()), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toEqual({
      total_count: 2,
      head_sha: HEAD,
      base_sha: BASE,
      complete: true,
      commits: [
        {
          sha: "c".repeat(40),
          subject: "Strip input",
          body: "Why it strips",
          author_login: "octo",
          author_name: "Octo",
          authored_at: "2026-11-20T09:00:00Z",
          committed_at: "2026-11-20T09:05:00Z",
          url: "https://github.com/acme/app/commit/c",
          ci_state: null,
        },
        {
          sha: HEAD,
          subject: "Describe it",
          body: null,
          author_login: null,
          author_name: "Octo",
          authored_at: "2026-11-20T10:00:00Z",
          committed_at: null,
          url: null,
          ci_state: null,
        },
      ],
    });
    expect(host.read.mock.calls[0]![0]).toBe("s1");
    expect(await sentRequest(0)).toEqual({
      method: "listPullRequestCommits",
      request: { prUrl: PR, headSha: HEAD },
    });
    expect(host.fastTrack).not.toHaveBeenCalled();
  });

  it("is never the whole history when the host cut it short or dropped a commit", async () => {
    host.read.mockResolvedValueOnce({
      has_more: true,
      total_count: 300,
      data: [{ sha: HEAD, title: "One" }],
    });
    const cut = renderHook(() => usePullRequestCommits(source()), { wrapper: wrapper() });
    await waitFor(() => expect(cut.result.current.data).toBeDefined());
    expect(cut.result.current.data).toMatchObject({ total_count: 300, complete: false });

    host.read.mockResolvedValueOnce({
      has_more: false,
      data: [{ title: "No sha" }, { sha: HEAD }],
    });
    const dropped = renderHook(() => usePullRequestCommits(source("s2")), { wrapper: wrapper() });
    await waitFor(() => expect(dropped.result.current.data).toBeDefined());
    expect(dropped.result.current.data).toMatchObject({ total_count: 1, complete: false });
  });

  it("keeps each session's list apart in the cache", () => {
    expect(pullRequestCommitsQueryKey(source("s1"))).not.toEqual(
      pullRequestCommitsQueryKey(source("s2")),
    );
  });

  it("reads one commit's patch through the host", async () => {
    host.read.mockResolvedValue({ object: "session.github.pr_diff", patch: "diff --git a b" });
    const sha = "c".repeat(40);
    const { result } = renderHook(() => useCommitDiff(source(), sha), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toEqual({ sha, patch: "diff --git a b" });
    expect(await sentRequest(0)).toEqual({
      method: "getCommitDiff",
      request: { prUrl: PR, commitSha: sha },
    });
  });
});
