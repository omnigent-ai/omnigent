// A list row's Merge for a PR in GitHub's own stack: GitHub merges the stack together, so the
// row's menu and ⌘K open the PR there, and a merge asked for before GitHub said so never sends.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import type * as OpenLinkInApp from "@/lib/openLinkInApp";
import type * as PageRouteModule from "@/pullRequestPage/pullRequestPageRoute";
import type { NativeStackMergeHandoff } from "@/pullRequestPage/nativeStackMerge";
import { PullRequestRowDestinationContext } from "@/pullRequestPage/pullRequestDestination";

import {
  type PullRequestListEntries,
  usePullRequestListEntries,
} from "./usePullRequestListEntries";

vi.mock("@/hooks/usePermissions", () => ({
  useCanEdit: () => true,
  useCanChangePullRequests: () => true,
}));
// The session's host reads merge options and merges.
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<typeof PageRouteModule>()),
  usePullRequestMethodServed: (_sessionId: string, method: string) =>
    method === "getMergeOptions" || method === "mergePullRequest",
}));
const opened = vi.hoisted(() => [] as string[]);
vi.mock("@/lib/openLinkInApp", async (importOriginal) => ({
  ...(await importOriginal<typeof OpenLinkInApp>()),
  openLink: (_id: string | undefined, url: string) => opened.push(url),
}));

const PR = "https://github.com/acme/app/pull/7";
const SCOPE = "pull-requests-page";
const ASSOCIATION = {
  url: PR,
  host: "github.com",
  repository: "acme/app",
  number: 7,
  relationship: "created",
} as unknown as PullRequestAssociation;

/**
 * The list's entries over one mergeable PR, its merge dialog rendered. `handoff` is what the
 * caller's read says GitHub merges; change it and `refresh`.
 */
function renderRow() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  client.setQueryData(["pr-page-github-info", SCOPE, PR], {
    object: "session.github.info",
    available: true,
    selected_pr_url: PR,
    capabilities: { merging: true },
    pr: {
      number: 7,
      title: "Build",
      state: "OPEN",
      url: PR,
      is_draft: false,
      author: "carol",
      base_ref: "main",
      head_ref: "feature",
      head_sha: "a".repeat(40),
      merge_state_status: "CLEAN",
      checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
    },
  });
  const state: { handoff: NativeStackMergeHandoff | null } = { handoff: null };
  const latest: { entries: PullRequestListEntries | null } = { entries: null };
  function List({ handoff }: { handoff: NativeStackMergeHandoff | null }) {
    const entries = usePullRequestListEntries({
      conversationId: SCOPE,
      active: false,
      associations: [ASSOCIATION],
      hostOffline: false,
      sandboxAsleep: false,
      onSelect: () => {},
      unlinkPending: false,
      managesSessionLinks: false,
      mergeHandoffOf: (url) => (url === PR ? handoff : null),
    });
    latest.entries = entries;
    return entries.mergeDialog;
  }
  const ui = () => (
    <QueryClientProvider client={client}>
      <SidebarDataProvider>
        <PullRequestRowDestinationContext.Provider value="page-detail">
          <List handoff={state.handoff} />
        </PullRequestRowDestinationContext.Provider>
      </SidebarDataProvider>
    </QueryClientProvider>
  );
  const view = render(ui());
  const merge = () =>
    latest.entries!.actionsFor(latest.entries!.entries[0]!).find((action) => action.id === "merge");
  return { state, merge, refresh: () => view.rerender(ui()) };
}

const mergeDialog = () => screen.queryByText("Merge pull request #7?");

beforeEach(() => {
  opened.length = 0;
});
afterEach(cleanup);

describe("a list row's Merge for a PR in GitHub's own stack", () => {
  it("opens the PR on GitHub from the row's menu and ⌘K, never asking to merge here", () => {
    const { state, merge, refresh } = renderRow();
    state.handoff = { url: PR, unmergedBelow: 2 };
    refresh();
    expect(merge()).toMatchObject({
      label: "Merge on GitHub",
      paletteLabel: "Merge #7 Build on GitHub",
      description:
        "GitHub merges a stack together: merging this pull request also merges the 2 unmerged pull requests below it.",
    });
    act(() => merge()!.run());
    expect(opened).toEqual([PR]);
    expect(mergeDialog()).toBeNull();
  });

  it("says GitHub may merge more where its read of the stack is partial", () => {
    const { state, merge, refresh } = renderRow();
    state.handoff = { url: PR, unmergedBelow: null };
    refresh();
    expect(merge()).toMatchObject({
      label: "Merge on GitHub",
      description:
        "GitHub merges a stack together: merging this pull request also merges any unmerged pull requests below it.",
    });
  });

  it("hands off from a Merge… kept from before GitHub said it stacks the PR", () => {
    const { state, merge, refresh } = renderRow();
    const retained = merge()!;
    expect(retained.label).toBe("Merge…");
    state.handoff = { url: PR, unmergedBelow: null };
    refresh();
    act(() => retained.run());
    expect(opened).toEqual([PR]);
    expect(mergeDialog()).toBeNull();
  });

  it.each([
    ["the whole stack", 1],
    ["part of the stack", null],
  ])(
    "retires an open merge once GitHub's read of %s comes in, for good",
    (_label, unmergedBelow) => {
      const { state, merge, refresh } = renderRow();
      act(() => merge()!.run());
      expect(mergeDialog()).not.toBeNull();
      state.handoff = { url: PR, unmergedBelow };
      refresh();
      expect(mergeDialog()).toBeNull();
      // GitHub saying otherwise later never brings the retired merge back.
      state.handoff = null;
      refresh();
      expect(mergeDialog()).toBeNull();
      expect(opened).toEqual([]);
    },
  );
});
