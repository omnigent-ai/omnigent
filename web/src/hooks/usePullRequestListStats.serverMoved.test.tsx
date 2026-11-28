// List stats the shell's fast track reads stop once the page's server moves, while the host's
// batches keep reading.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import type * as BasePathModule from "@/lib/basePath";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

const state = vi.hoisted(() => ({ server: "https://server-a.example.com", hostRead: vi.fn() }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => state.server,
}));
vi.mock("@/lib/basePath", async (importOriginal) => ({
  ...(await importOriginal<typeof BasePathModule>()),
  getApiBasePath: () => "/api",
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: state.hostRead,
}));

import { usePullRequestListStats } from "@/hooks/usePullRequestListStats";
import type { PullRequestListStatsTarget } from "@/lib/pullRequestListStats";
import {
  githubFastTrackServerMoved,
  resetGitHubFastTrackServerForTests,
} from "@/lib/githubFastTrackServer";

const FAST = "https://github.com/acme/app/pull/1";
const HOSTED = "https://github.com/acme/app/pull/2";

const entry = (repository: string, number: number) => ({
  repository,
  number,
  additions: 3,
  deletions: 1,
  changed_files: 1,
  updated_at: "2026-11-24T10:00:00Z",
  head_sha: "head",
  base_sha: "base",
  state: "OPEN",
  is_draft: false,
});

const TARGETS: PullRequestListStatsTarget[] = [
  {
    url: FAST,
    source: { kind: "github-fast-track", account: { host: "github.com", login: "octo" } },
    repository: "acme/app",
    number: 1,
    headSha: undefined,
    open: true,
  },
  {
    url: HOSTED,
    source: { kind: "host", sessionId: "s1", host: "github.com" },
    repository: "acme/app",
    number: 2,
    headSha: undefined,
    open: true,
  },
];

afterEach(() => {
  resetGitHubFastTrackServerForTests();
  state.server = "https://server-a.example.com";
  state.hostRead.mockReset();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

it("reads no stats on this Mac after the server moves, and keeps reading through the host", async () => {
  const githubQuery = vi.fn(async () => ({
    ok: true,
    data: { reader: "octo", pull_requests: [entry("acme/app", 1)] },
  }));
  (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
    kind: "electron",
    getDesktopFeatures: async () => ({
      githubFastTrack: true,
      githubFastTrackOperations: ["account_status", "pull_request_list_stats"],
    }),
    githubQuery,
  };
  state.hostRead.mockResolvedValue({ reader: "octo", data: [entry("acme/app", 2)] });
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(
    () => usePullRequestListStats(TARGETS, { enabled: true, fastTrackSettled: true }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.byUrl.size).toBe(2));
  const sent = githubQuery.mock.calls.length;

  state.server = "https://server-b.example.com";
  await act(async () => {
    githubFastTrackServerMoved();
    await Promise.resolve();
  });
  await act(() => client.invalidateQueries());
  await waitFor(() =>
    expect(result.current.gaps.get(FAST)).toEqual({ kind: "unavailable", service: "github" }),
  );
  expect(result.current.byUrl.has(HOSTED)).toBe(true);
  expect(state.hostRead.mock.calls.length).toBeGreaterThan(1);
  expect(githubQuery.mock.calls.length).toBe(sent);
});
