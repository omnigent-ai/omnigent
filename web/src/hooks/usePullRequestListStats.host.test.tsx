// List stats read by the session's host: one GetPullRequestListStats per batch, with the
// PRs' URLs, and each row matched to its answer by repository and number.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ read: vi.fn() }));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: host.read,
}));

import { usePullRequestListStats } from "@/hooks/usePullRequestListStats";
import type { PullRequestListStatsTarget } from "@/lib/pullRequestListStats";
import { PullRequestApiError } from "@/lib/pullRequestRpc";
import type { WorkspaceClient } from "@/lib/workspaceApi";

const ONE = "https://github.com/acme/app/pull/1";
const TWO = "https://github.com/acme/app/pull/2";

const target = (url: string, number: number): PullRequestListStatsTarget => ({
  url,
  source: { kind: "host", sessionId: "s1", host: "github.com" },
  repository: "acme/app",
  number,
  headSha: "head",
  open: true,
});

const entry = (number: number, additions: number) => ({
  repository: "Acme/App",
  number,
  additions,
  deletions: 1,
  changed_files: 1,
  updated_at: "2026-11-24T10:00:00Z",
  head_sha: "head",
  base_sha: "base",
  state: "OPEN",
  is_draft: false,
  ci: "passing",
  ci_head_sha: "head",
});

function run(targets: PullRequestListStatsTarget[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    () => usePullRequestListStats(targets, { enabled: true, fastTrackSettled: true }),
    { wrapper },
  );
}

beforeEach(() => {
  host.read.mockReset();
});

describe("list stats through the session's host", () => {
  it("reads one batch by URL and matches each answer to its row, whatever its order", async () => {
    host.read.mockResolvedValue({
      object: "list",
      reader: "octo",
      data: [entry(2, 20), entry(1, 10)],
    });
    const { result } = run([target(ONE, 1), target(TWO, 2)]);
    await waitFor(() => expect(result.current.byUrl.size).toBe(2));
    expect(result.current.byUrl.get(ONE)).toMatchObject({ additions: 10, ci: "passing" });
    expect(result.current.byUrl.get(TWO)).toMatchObject({ additions: 20, state: "OPEN" });
    expect(host.read).toHaveBeenCalledTimes(1);
    const [[session, method, call]] = host.read.mock.calls;
    expect([session, method]).toEqual(["s1", "getPullRequestListStats"]);
    const getPullRequestListStats = vi.fn(async () => ({ payload: {} }));
    await call({ getPullRequestListStats } as unknown as WorkspaceClient);
    expect(getPullRequestListStats).toHaveBeenCalledWith({ prUrls: [ONE, TWO] });
  });

  it("says a row the host left out wasn't reported", async () => {
    host.read.mockResolvedValue({ object: "list", reader: "octo", data: [entry(1, 10)] });
    const { result } = run([target(ONE, 1), target(TWO, 2)]);
    await waitFor(() => expect(result.current.byUrl.size).toBe(1));
    await waitFor(() =>
      expect(result.current.gaps.get(TWO)).toEqual({ kind: "not-reported", service: "github" }),
    );
  });

  it("shows the host's refusal on every row of its batch", async () => {
    host.read.mockRejectedValue(new PullRequestApiError("Sign in to github.com", 400));
    const { result } = run([target(ONE, 1)]);
    await waitFor(() =>
      expect(result.current.gaps.get(ONE)).toEqual({
        kind: "read-failed",
        service: "github",
        code: "http_400",
        message: "Sign in to github.com",
      }),
    );
  });
});
