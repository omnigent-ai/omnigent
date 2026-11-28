// A list reads its rows' stats through the session's host where it serves them. Elsewhere a
// shell with the GitHub fast track reads them, until the page's server moves.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import type * as BasePathModule from "@/lib/basePath";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import type * as ListStatsModule from "@/hooks/usePullRequestListStats";
import type * as PullRequestsModule from "@/newPullRequestPage/hooks/usePullRequests";
import type { PullRequestListStatsTarget } from "@/lib/pullRequestListStats";

const state = vi.hoisted(() => ({
  server: "https://server-a.example.com",
  hostServesStats: true,
  targets: [] as readonly PullRequestListStatsTarget[],
}));
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
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (_session: string, method: string) =>
    method !== "getPullRequestListStats" || state.hostServesStats,
}));
// The targets the list hands its stats reads, so a case sees which source each row reads by.
vi.mock("@/hooks/usePullRequestListStats", async (importOriginal) => {
  const actual = await importOriginal<typeof ListStatsModule>();
  return {
    ...actual,
    usePullRequestListStats: (...args: Parameters<typeof actual.usePullRequestListStats>) => {
      state.targets = args[0];
      return actual.usePullRequestListStats(...args);
    },
  };
});
vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => {
  const actual = await importOriginal<typeof PullRequestsModule>();
  return {
    ...actual,
    usePullRequestInfos: (_sessionId: string, urls: readonly string[]) =>
      urls.map(() => ({
        data: {
          object: "session.github.info",
          available: true,
          provider: "github",
          tracking_available: true,
          prs: [],
          selected_pr_url: PR,
          auth: {
            authenticated: true,
            hint: null,
            cli: null,
            accounts: [],
            selected_account: "octo",
          },
          capabilities: {},
          pr: {
            number: 7,
            title: "Seven",
            state: "OPEN",
            url: PR,
            is_draft: false,
            author: "author",
            base_ref: "main",
            head_ref: "feature",
            head_sha: "a".repeat(40),
            checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
            comments: [],
            reviewers: [],
            labels: [],
          },
        },
        error: null,
        isError: false,
        isFetching: false,
        isLoading: false,
        isEnabled: true,
        isStale: false,
        dataUpdatedAt: 1,
        refetch: async () => undefined,
      })),
  };
});

import { SidebarDataProvider } from "@/hooks/useSidebarData";
import {
  githubFastTrackServerMoved,
  resetGitHubFastTrackServerForTests,
} from "@/lib/githubFastTrackServer";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";

import { usePullRequestListEntries } from "./usePullRequestListEntries";

const PR = "https://github.com/acme/app/pull/7";
const association = {
  url: PR,
  host: "github.com",
  repository: "acme/app",
  number: 7,
  relationship: "created",
} as unknown as PullRequestAssociation;

afterEach(() => {
  resetGitHubFastTrackServerForTests();
  state.server = "https://server-a.example.com";
  state.hostServesStats = true;
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

function renderList() {
  const githubQuery = vi.fn(async () => ({ ok: true, data: {} }));
  (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
    kind: "electron",
    githubQuery,
    getDesktopFeatures: async () => ({ githubFastTrack: true, githubFastTrackOperations: [] }),
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <SidebarDataProvider>{children}</SidebarDataProvider>
    </QueryClientProvider>
  );
  renderHook(
    () =>
      usePullRequestListEntries({
        conversationId: "session-1",
        active: false,
        associations: [association],
        hostOffline: false,
        sandboxAsleep: false,
        onSelect: () => {},
        unlinkPending: false,
        managesSessionLinks: false,
      }),
    { wrapper },
  );
  return githubQuery;
}

const sources = () => state.targets.map((target) => target.source.kind);

async function moveServer() {
  state.server = "https://server-b.example.com";
  await act(async () => {
    githubFastTrackServerMoved();
    await Promise.resolve();
  });
}

it("reads a row's stats through the session's host where it serves them, never the fast track", async () => {
  const githubQuery = renderList();
  expect(sources()).toEqual(["host"]);
  await moveServer();
  expect(sources()).toEqual(["host"]);
  expect(githubQuery).not.toHaveBeenCalled();
});

it("reads on the fast track where the host doesn't serve stats, and nowhere once the server moves", async () => {
  state.hostServesStats = false;
  const githubQuery = renderList();
  expect(githubFastTrackServerMoved()).toBe(false);
  expect(sources()).toEqual(["github-fast-track"]);
  await moveServer();
  expect(sources()).toEqual([]);
  expect(githubQuery).not.toHaveBeenCalled();
});
