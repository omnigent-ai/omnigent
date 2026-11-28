// A sub-agent's row in a session's list offers Remove: in a tree the user owns it names the
// agents it unlinks the PR from; elsewhere it stays, disabled, saying who still links it.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type * as Permissions from "@/hooks/usePermissions";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import type { PullRequestSessionSource } from "@/pullRequestList/subAgentPullRequests";
import { pullRequestKey } from "@/lib/pullRequestReference";
import type { RemovalProgress } from "@/pullRequestList/removalProgress";
import { isCommandAvailable, unavailableBecause } from "@/store/paletteCommands";

import { type PullRequestListProps, usePullRequestListEntries } from "./usePullRequestListEntries";

vi.mock("@/hooks/usePermissions", async (importOriginal) => ({
  ...(await importOriginal<typeof Permissions>()),
  useCanEdit: () => true,
  useCanChangePullRequests: () => true,
}));

const PR = "https://github.com/acme/app/pull/7";
const AUTH = { id: "conv_auth", name: "auth-fix" };
const DOCS = { id: "conv_docs", name: "docs" };

function removeFor(source: PullRequestSessionSource, over: Partial<PullRequestListProps> = {}) {
  const onUnlink = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <SidebarDataProvider>{children}</SidebarDataProvider>
    </QueryClientProvider>
  );
  const association = {
    url: PR,
    host: "github.com",
    repository: "acme/app",
    number: 7,
    relationship: "created",
  } as unknown as PullRequestAssociation;
  const { result } = renderHook(
    () =>
      usePullRequestListEntries({
        conversationId: "conv_parent",
        active: false,
        associations: [association],
        sessionSources: new Map([[PR, source]]),
        hostOffline: true,
        sandboxAsleep: false,
        onSelect: vi.fn(),
        onUnlink,
        unlinkPending: false,
        ...over,
      }),
    { wrapper },
  );
  const actions = result.current.actionsFor(result.current.entries[0]!);
  return {
    onUnlink,
    remove: actions.find((action) => action.id === "remove"),
    hook: result.current,
  };
}

it("names the one sub-agent it unlinks the PR from, and removes it", () => {
  const { onUnlink, remove } = removeFor({ session: AUTH, alsoIn: [] }, { ownsSessionTree: true });
  expect(remove).toMatchObject({
    label: "Remove from auth-fix",
    paletteLabel: "Remove #7 from auth-fix",
  });
  expect(isCommandAvailable(remove!.availability)).toBe(true);
  remove!.run();
  expect(onUnlink).toHaveBeenCalledExactlyOnceWith([PR]);
});

it("counts and names every agent it unlinks a PR several of them link from", () => {
  const { remove } = removeFor({ session: null, alsoIn: [AUTH, DOCS] }, { ownsSessionTree: true });
  expect(remove).toMatchObject({
    label: "Remove from 3 agents",
    description: "Unlinks it from this session, auth-fix and docs.",
  });
});

it.each([
  [false, "Still linked by auth-fix, which you don’t own."],
  [undefined, "Checking whether you own auth-fix…"],
])("stays disabled, saying why, where the user's ownership of the tree is %s", (owns, reason) => {
  const { onUnlink, remove } = removeFor({ session: AUTH, alsoIn: [] }, { ownsSessionTree: owns });
  expect(remove?.label).toBe("Remove from this session");
  expect(remove?.availability).toEqual(expect.objectContaining({ available: false, reason }));
  remove!.run();
  expect(onUnlink).not.toHaveBeenCalled();
});

it("removes this session's own link and says which agents still link it", () => {
  const { onUnlink, remove } = removeFor(
    { session: null, alsoIn: [AUTH] },
    { ownsSessionTree: false },
  );
  expect(remove).toMatchObject({
    label: "Remove from this session",
    description: "Still linked by auth-fix, which you don’t own.",
  });
  remove!.run();
  expect(onUnlink).toHaveBeenCalledExactlyOnceWith([PR]);
});

it("gives no removal for a URL the list doesn't show, so a stale command removes nothing", () => {
  const { hook } = removeFor({ session: AUTH, alsoIn: [] }, { ownsSessionTree: true });
  expect(hook.removalOf("https://github.com/acme/app/pull/99")).toBeUndefined();
  expect(hook.removalOf(`${PR}/`)?.targets).toEqual([{ sessionId: AUTH.id, url: PR }]);
});

describe("while a removal is under way", () => {
  const removing = (over: Partial<RemovalProgress>): RemovalProgress => ({
    phase: "removing",
    urls: new Set([pullRequestKey(PR)]),
    done: 0,
    total: 1,
    stop: "none",
    runId: null,
    ...over,
  });

  it("says how far the row's own removal got, in its menu and on the row", () => {
    const { remove, hook } = removeFor(
      { session: null, alsoIn: [] },
      { unlinkPending: true, removalProgress: removing({}) },
    );
    expect(remove?.availability).toEqual(unavailableBecause("Removing this pull request…"));
    expect(hook.changeInFlightFor(PR)).toBe("Removing…");
    expect(hook.unlinkFinishedCommand).toBeUndefined();
  });

  it("asks another row to wait for the batch, which it isn't part of", () => {
    const { remove, hook } = removeFor(
      { session: null, alsoIn: [] },
      {
        unlinkPending: true,
        removalProgress: removing({
          urls: new Set([pullRequestKey("https://github.com/acme/app/pull/8")]),
          done: 1,
          total: 3,
          stop: "available",
        }),
      },
    );
    expect(remove?.availability).toEqual(
      unavailableBecause("Wait for the 3 pull requests being removed to finish."),
    );
    expect(hook.changeInFlightFor(PR)).toBeNull();
  });

  it("marks no row as being removed while its removal is only queued", () => {
    const { remove, hook } = removeFor(
      { session: null, alsoIn: [] },
      { unlinkPending: true, removalProgress: removing({ phase: "waiting", done: null }) },
    );
    expect(remove?.availability).toEqual(
      unavailableBecause("Waiting for another removal to finish."),
    );
    expect(hook.changeInFlightFor(PR)).toBeNull();
  });
});
