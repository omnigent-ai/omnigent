// The session panel's loading preview of a PR, from its summary under the reader the overview
// reads as: gone when the workspace user changes (heard through the identity subscription, as
// the app hears it) or when this Mac reads as another GitHub account.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

const identity = vi.hoisted(() => ({
  userId: "user-1" as string | null,
  listeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => identity.userId,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => identity.listeners.delete(listener);
  },
}));

import {
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import {
  acceptMacRead,
  beginMacRead,
  macReadScopeKey,
  resetMacReadScopesForTests,
} from "@/lib/macReadScope";
import { PULL_REQUEST_SUMMARY_FAMILY, publishPullRequestSummaries } from "@/lib/pullRequestRecord";

import {
  LISTED_COMMITS_MAX_AGE_MS,
  listedPatchRevision,
  useListedPatchRevision,
  usePullRequestSummaryPreview,
} from "./usePullRequestSummaryPreview";

const PR = "https://github.com/acme/app/pull/77";
const scope = (userId = "user-1") => macReadScopeKey("github", "github.com", userId);

beforeEach(async () => {
  identity.userId = "user-1";
  resetMacReadScopesForTests();
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(() => new Promise(() => {})),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS],
          accounts: true,
          answerProvenance: 1,
        },
      ],
    }),
  };
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  identity.listeners.clear();
});

/** The inbox read as alice, kept as the PR's summary, with the preview mounted for a session. */
async function aliceSummaryShown() {
  const client = new QueryClient();
  const ticket = beginMacRead(scope());
  acceptMacRead(ticket, "alice");
  publishPullRequestSummaries(client, { viewer: "alice", ticket }, [
    { url: PR, fields: { title: "Faster first frame", state: "OPEN", author: "amy" } },
  ]);
  const view = renderHook(() => usePullRequestSummaryPreview("conv_1", PR), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  await waitFor(() => expect(view.result.current?.title).toBe("Faster first frame"));
  return { client, view };
}

const summaries = (client: QueryClient) =>
  client.getQueryCache().findAll({ queryKey: [PULL_REQUEST_SUMMARY_FAMILY] });

describe("usePullRequestSummaryPreview", () => {
  it("shows nothing of the last workspace user's summaries once another signs in", async () => {
    const { client, view } = await aliceSummaryShown();
    expect(identity.listeners.size).toBeGreaterThan(0);
    act(() => {
      identity.userId = "user-2";
      for (const listener of identity.listeners) listener();
    });
    expect(view.result.current).toBeUndefined();
    expect(summaries(client)).toEqual([]);
  });

  it("shows nothing of Alice's summary once this Mac reads as Bob", async () => {
    const { client, view } = await aliceSummaryShown();
    act(() => {
      acceptMacRead(beginMacRead(scope()), "bob");
    });
    expect(view.result.current).toBeUndefined();
    expect(summaries(client)).toEqual([]);
  });
});

describe("listedPatchRevision", () => {
  const listed = (readAt: number) => ({
    listedCommits: { value: { base: "base", head: "head", readAt }, sequence: 1 },
  });

  it("keys the patch on a recent list read's base and head", () => {
    expect(listedPatchRevision(listed(1_000), 1_000 + LISTED_COMMITS_MAX_AGE_MS)).toBe("base:head");
  });

  it("starts nothing from a read that may predate a push, or without listed commits", () => {
    expect(listedPatchRevision(listed(1_000), 1_001 + LISTED_COMMITS_MAX_AGE_MS)).toBeUndefined();
    expect(listedPatchRevision({}, 0)).toBeUndefined();
    expect(listedPatchRevision(undefined, 0)).toBeUndefined();
  });
});

describe("useListedPatchRevision", () => {
  it("gives the listed commits under the reader the overview reads as, and none for another", async () => {
    const client = new QueryClient();
    const ticket = beginMacRead(scope());
    acceptMacRead(ticket, "alice");
    publishPullRequestSummaries(client, { viewer: "alice", ticket }, [
      { url: PR, fields: { listedCommits: { base: "base", head: "head", readAt: Date.now() } } },
    ]);
    const view = renderHook(() => useListedPatchRevision("conv_1", PR), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    await waitFor(() => expect(view.result.current).toBe("base:head"));
    act(() => {
      acceptMacRead(beginMacRead(scope()), "bob");
    });
    expect(view.result.current).toBeUndefined();
  });
});
