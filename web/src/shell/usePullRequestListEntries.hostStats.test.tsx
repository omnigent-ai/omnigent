// In a browser, a list whose session's host reads its rows' stats lays out the Diff and Updated
// columns for them; a host that doesn't, with no shell bridge either, leaves them out.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ servesStats: true }));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (sessionId: string, method: string) =>
    sessionId === "session-1" && method === "getPullRequestListStats" && host.servesStats,
}));

import { SidebarDataProvider } from "@/hooks/useSidebarData";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";

import { usePullRequestListEntries } from "./usePullRequestListEntries";

const association = {
  url: "https://github.com/acme/app/pull/7",
  host: "github.com",
  repository: "acme/app",
  number: 7,
  relationship: "created",
} as unknown as PullRequestAssociation;

function statsColumns(): boolean {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <SidebarDataProvider>{children}</SidebarDataProvider>
    </QueryClientProvider>
  );
  const { result } = renderHook(
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
  return result.current.statsColumns;
}

beforeEach(() => {
  host.servesStats = true;
});

describe("a browser list's stats columns", () => {
  it("are laid out where the session's host reads the rows' stats", () => {
    expect(statsColumns()).toBe(true);
  });

  it("stay out where neither the host nor a shell bridge reads them", () => {
    host.servesStats = false;
    expect(statsColumns()).toBe(false);
  });
});
