import { renderHook } from "@testing-library/react";
import type * as UseSessionPullRequestsModule from "@/hooks/useSessionPullRequests";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HostWorktree } from "@/hooks/useHostWorktrees";
import type { SessionWorktreesResult } from "@/hooks/useSessionWorktrees";
import { useComposerGitStatus } from "./useComposerGitStatus";

const useSessionWorktreesMock = vi.fn();
const usePullRequestInfoMock = vi.fn();
const usePullRequestInfosMock = vi.fn<(id: string | undefined, urls: string[]) => unknown[]>(
  () => [],
);
const useSessionPullRequestsMock = vi.fn(() => ({ data: undefined as unknown }));

vi.mock("@/hooks/useSessionWorktrees", () => ({
  useSessionWorktrees: (
    sessionId: string | null,
    hostId: string | null,
    workspace: string | null,
  ) => useSessionWorktreesMock(sessionId, hostId, workspace),
}));
vi.mock("@/hooks/usePullRequests", () => ({
  usePullRequestInfo: (id: string | undefined) => usePullRequestInfoMock(id),
  usePullRequestInfos: (id: string | undefined, urls: string[]) =>
    usePullRequestInfosMock(id, urls),
}));
vi.mock("@/hooks/useSessionPullRequests", async (importOriginal) => ({
  storedListToShow: (await importOriginal<typeof UseSessionPullRequestsModule>()).storedListToShow,
  useSessionPullRequests: () => useSessionPullRequestsMock(),
}));

function wt(overrides: Partial<HostWorktree>): HostWorktree {
  return {
    path: overrides.path ?? "/home/a/repo",
    branch: overrides.branch ?? null,
    is_main: overrides.is_main ?? true,
    detached: overrides.detached ?? false,
  };
}

function setWorktrees(
  data: SessionWorktreesResult | undefined,
  extra: Record<string, unknown> = {},
) {
  useSessionWorktreesMock.mockReturnValue({
    data,
    isLoading: false,
    isError: false,
    isFetching: false,
    isPlaceholderData: false,
    refetch: vi.fn(),
    ...extra,
  });
}
function setGithub(data: Record<string, unknown> | undefined, extra: Record<string, unknown> = {}) {
  usePullRequestInfoMock.mockReturnValue({ data, isFetching: false, refetch: vi.fn(), ...extra });
}

function run(args: Partial<Parameters<typeof useComposerGitStatus>[0]> = {}) {
  return renderHook(() =>
    useComposerGitStatus({
      sessionId: "conv-1",
      hostId: "host-1",
      workspace: "/home/a/repo",
      creationBranch: null,
      ...args,
    }),
  );
}

afterEach(() => {
  useSessionWorktreesMock.mockReset();
  usePullRequestInfoMock.mockReset();
  usePullRequestInfosMock.mockReset();
  usePullRequestInfosMock.mockReturnValue([]);
  useSessionPullRequestsMock.mockReset();
  useSessionPullRequestsMock.mockReturnValue({ data: undefined });
});

const FORGE_DISPLAY = {
  id: "example_forge",
  display_name: "Example Forge",
  request_name: "pull request",
  number_prefix: "!",
};

describe("useComposerGitStatus", () => {
  it("reports the live checkout branch of the main worktree", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ branch: "main", is_main: true })] });
    setGithub(undefined);
    const { result } = run();
    expect(result.current.branchState).toBe("branch");
    expect(result.current.branch).toBe("main");
    expect(result.current.isWorktree).toBe(false);
  });

  it("does NOT use the PR head ref as the branch", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ branch: "feature/local" })] });
    setGithub({ branch: "pr-head-ref", available: true });
    expect(run().result.current.branch).toBe("feature/local");
  });

  it("marks a linked worktree distinct from the main repo", () => {
    setWorktrees({
      status: "ok",
      worktrees: [
        wt({ path: "/home/a/repo", branch: "main", is_main: true }),
        wt({ path: "/home/a/repo-wt/feat", branch: "feat", is_main: false }),
      ],
    });
    setGithub(undefined);
    const { result } = run({ workspace: "/home/a/repo-wt/feat" });
    expect(result.current.isWorktree).toBe(true);
    expect(result.current.worktreePath).toBe("/home/a/repo-wt/feat");
    expect(result.current.branch).toBe("feat");
  });

  it("matches a workspace bound to a subdir of a worktree", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ path: "/home/a/repo", branch: "main" })] });
    setGithub(undefined);
    expect(run({ workspace: "/home/a/repo/packages/web" }).result.current.branch).toBe("main");
  });

  it("matches a Windows nested workspace across separator and case differences", () => {
    // Host returns `git worktree list` paths verbatim (forward slash, its own
    // case); the session workspace can use backslashes and different case.
    setWorktrees({ status: "ok", worktrees: [wt({ path: "C:/repo", branch: "main" })] });
    setGithub(undefined);
    const { result } = run({ workspace: "C:\\Repo\\packages\\web" });
    expect(result.current.branchState).toBe("branch");
    expect(result.current.branch).toBe("main");
    expect(result.current.isWorktree).toBe(false);
  });

  it("matches a Windows worktree exactly despite separator/case skew", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ path: "C:\\Repo", branch: "feat" })] });
    setGithub(undefined);
    expect(run({ workspace: "c:/repo" }).result.current.branch).toBe("feat");
  });

  it("does not match a sibling whose path is a mere prefix", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ path: "/home/a/repo", branch: "main" })] });
    setGithub(undefined);
    expect(run({ workspace: "/home/a/repo-sibling" }).result.current.branchState).toBe("unknown");
  });

  it("does not treat a backslash as a separator on POSIX", () => {
    // On POSIX `\` is a legal filename char, so `/home/a/repo\evil` is not a
    // child of `/home/a/repo`.
    setWorktrees({ status: "ok", worktrees: [wt({ path: "/home/a/repo", branch: "main" })] });
    setGithub(undefined);
    expect(run({ workspace: "/home/a/repo\\evil" }).result.current.branchState).toBe("unknown");
  });

  it("models detached HEAD as detached, not a fake branch", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ branch: null, detached: true })] });
    setGithub(undefined);
    const { result } = run();
    expect(result.current.branchState).toBe("detached");
    expect(result.current.branch).toBeNull();
  });

  it("reports not-git only on explicit non-repo evidence", () => {
    setWorktrees({ status: "not_git" });
    setGithub(undefined);
    expect(run().result.current.branchState).toBe("not-git");
  });

  it("treats an OK-but-empty list as unknown, never not-git", () => {
    // useHostWorktrees collapses 400 → []; an empty ok list is not proof of non-git.
    setWorktrees({ status: "ok", worktrees: [] });
    setGithub(undefined);
    expect(run().result.current.branchState).toBe("unknown");
  });

  it("treats an ambiguous git failure as unknown", () => {
    setWorktrees({ status: "unknown" });
    setGithub(undefined);
    expect(run().result.current.branchState).toBe("unknown");
  });

  it("reports loading while the worktree query is in flight", () => {
    setWorktrees(undefined, { isLoading: true });
    setGithub(undefined);
    expect(run().result.current.branchState).toBe("loading");
  });

  it("never advertises placeholder (stale prior-workspace) data as live", () => {
    // A host/workspace switch surfaces the previous key's data as placeholder;
    // it must read as loading, not the old branch.
    setWorktrees(
      { status: "ok", worktrees: [wt({ branch: "old-workspace-branch" })] },
      { isPlaceholderData: true },
    );
    setGithub(undefined);
    const { result } = run();
    expect(result.current.branchState).toBe("loading");
    expect(result.current.branch).toBeNull();
  });

  it("reports unknown with no host", () => {
    setWorktrees(undefined);
    setGithub(undefined);
    expect(run({ hostId: null }).result.current.branchState).toBe("unknown");
  });

  it("never substitutes the creation branch for the live branch", () => {
    setWorktrees({ status: "unknown" });
    setGithub(undefined);
    const { result } = run({ creationBranch: "stale/creation" });
    expect(result.current.branch).toBeNull();
    expect(result.current.branchState).toBe("unknown");
    expect(result.current.creationBranch).toBe("stale/creation");
  });

  it("derives PR count/number from the github associations", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ branch: "main" })] });
    setGithub({
      prs: [
        { url: "u1", host: "h", repository: "r", number: 7, relationship: "created" },
        { url: "u2", host: "h", repository: "r", number: 9, relationship: "attached" },
      ],
    });
    const { result } = run();
    expect(result.current.prCount).toBe(2);
    expect(result.current.prNumber).toBe(7);
  });

  it("prioritizes every linked PR's live status instead of the selected or first PR", () => {
    setWorktrees(undefined);
    const prs = [
      { url: "merged", number: 1, state: "MERGED" },
      { url: "closed", number: 2, state: "CLOSED" },
      {
        url: "ready",
        number: 3,
        state: "OPEN",
        provider: "example_forge",
        provider_display: FORGE_DISPLAY,
      },
      { url: "pending", number: 4, state: "OPEN" },
      { url: "conflict", number: 5, state: "OPEN" },
    ];
    setGithub({
      tracking_available: true,
      prs,
      pr: { url: "ready", number: 3, state: "OPEN", merge_state_status: "CLEAN" },
    });
    usePullRequestInfosMock.mockReturnValue([
      { data: { pr: { url: "closed", state: "CLOSED" } } },
      { data: { pr: { url: "pending", state: "OPEN", checks: { pending: 1 } } } },
      { data: { pr: { url: "conflict", state: "OPEN", merge_state_status: "DIRTY" } } },
    ]);
    const { result } = run();
    expect(result.current.prs.map((pr) => pr.number)).toEqual([5, 3, 4, 1, 2]);
    expect(result.current).toMatchObject({ prCount: 5, prNumber: 5, prStatus: "conflict" });
    expect(usePullRequestInfosMock).toHaveBeenCalledWith("conv-1", [
      "closed",
      "pending",
      "conflict",
    ]);
  });

  it("refreshes a closed PR that has reopened while leaving merged PRs at rest", () => {
    setWorktrees(undefined);
    setGithub({ tracking_available: true });
    useSessionPullRequestsMock.mockReturnValue({
      data: {
        synced: true,
        prs: [
          { url: "merged", number: 1, state: "merged" },
          { url: "reopened", number: 2, state: "closed" },
        ],
      },
    });
    usePullRequestInfosMock.mockReturnValue([
      { data: { pr: { url: "reopened", state: "OPEN", merge_state_status: "CLEAN" } } },
    ]);
    expect(run().result.current).toMatchObject({ prNumber: 2, prStatus: "ready" });
    expect(usePullRequestInfosMock).toHaveBeenCalledWith("conv-1", ["reopened"]);
  });

  it("does not restore a removed selected PR when the stored list is empty", () => {
    setWorktrees(undefined);
    setGithub({
      tracking_available: true,
      prs: [{ url: "removed", number: 1, state: "OPEN" }],
      pr: { url: "removed", number: 1, state: "OPEN", merge_state_status: "DIRTY" },
    });
    useSessionPullRequestsMock.mockReturnValue({ data: { prs: [], synced: true } });
    expect(run().result.current).toMatchObject({
      prs: [],
      prCount: 0,
      prNumber: null,
      prStatus: null,
    });
    expect(usePullRequestInfosMock).toHaveBeenCalledWith("conv-1", []);
  });

  it.each([{ isError: true }, { isEnabled: false }])(
    "drops stale readiness and conflicts after a failed refresh or offline transition: %j",
    (queryState) => {
      setWorktrees(undefined);
      setGithub(
        {
          prs: [
            { url: "ready", number: 1, state: "OPEN" },
            { url: "conflict", number: 2, state: "OPEN" },
          ],
          pr: { url: "ready", state: "OPEN", merge_state_status: "CLEAN" },
        },
        queryState,
      );
      usePullRequestInfosMock.mockReturnValue([
        {
          ...queryState,
          data: { pr: { url: "conflict", state: "OPEN", merge_state_status: "DIRTY" } },
        },
      ]);
      expect(run().result.current).toMatchObject({ prStatus: "open", prNumber: 1 });
    },
  );

  it("ignores a detail response for a different requested PR URL", () => {
    setWorktrees(undefined);
    setGithub({
      prs: [
        { url: "first", number: 1, state: "OPEN" },
        { url: "second", number: 2, state: "OPEN" },
      ],
    });
    usePullRequestInfosMock.mockReturnValue([
      { data: { pr: { url: "second", number: 2, state: "OPEN", merge_state_status: "DIRTY" } } },
      { data: { pr: null } },
    ]);
    expect(run().result.current).toMatchObject({ prStatus: "open", prNumber: 1 });
  });

  it("keeps known terminal states offline unless the stored lifecycle is newer", () => {
    setWorktrees(undefined);
    setGithub({ tracking_available: true });
    useSessionPullRequestsMock.mockReturnValue({
      data: {
        synced: true,
        prs: [{ url: "closed", number: 1, state: "OPEN", state_checked_at: 1 }],
      },
    });
    usePullRequestInfosMock.mockReturnValue([
      { isEnabled: false, dataUpdatedAt: 2000, data: { pr: { url: "closed", state: "CLOSED" } } },
    ]);
    const { result, rerender } = run();
    expect(result.current.prStatus).toBe("closed");
    useSessionPullRequestsMock.mockReturnValue({
      data: {
        synced: true,
        prs: [{ url: "closed", number: 1, state: "OPEN", state_checked_at: 3 }],
      },
    });
    rerender();
    expect(result.current.prStatus).toBe("open");
  });

  it.each(["selected", "linked"])(
    "uses newer stored lifecycle over an older successful %s PR response",
    (source) => {
      setWorktrees(undefined);
      const pr = { url: "updated", state: "OPEN", merge_state_status: "CLEAN", title: "Old title" };
      const info = { tracking_available: true, ...(source === "selected" ? { pr } : {}) };
      setGithub(info, { dataUpdatedAt: 2000 });
      if (source === "linked") {
        usePullRequestInfosMock.mockReturnValue([{ data: { pr }, dataUpdatedAt: 2000 }]);
      }
      useSessionPullRequestsMock.mockReturnValue({
        data: {
          synced: true,
          prs: [
            {
              url: "updated",
              number: 1,
              state: "CLOSED",
              title: "Current title",
              state_checked_at: 3,
            },
          ],
        },
      });
      const { result, rerender } = run();
      expect(result.current).toMatchObject({
        prStatus: "closed",
        prs: [{ title: "Current title" }],
      });
      expect(result.current.prs[0].merge_state_status).toBeUndefined();

      setGithub(info, { dataUpdatedAt: 4000 });
      if (source === "linked") {
        usePullRequestInfosMock.mockReturnValue([{ data: { pr }, dataUpdatedAt: 4000 }]);
      }
      rerender();
      expect(result.current.prStatus).toBe("ready");
    },
  );

  it.each([
    { live: { merge_state_status: "DIRTY" }, storedDraft: undefined, expected: "conflict" },
    { live: { checks: { failing: 1, pending: 0 } }, storedDraft: undefined, expected: "failed" },
    { live: { merge_state_status: "CLEAN" }, storedDraft: undefined, expected: "ready" },
    {
      live: { is_draft: true, merge_state_status: "CLEAN" },
      storedDraft: undefined,
      expected: "draft",
    },
    {
      live: { is_draft: false, merge_state_status: "CLEAN" },
      storedDraft: true,
      expected: "draft",
    },
    { live: { is_draft: true, merge_state_status: "CLEAN" }, storedDraft: false, expected: "open" },
  ])(
    "retains rich status unless a newer summary changes draft lifecycle: $expected",
    ({ live, storedDraft, expected }) => {
      setWorktrees(undefined);
      setGithub({ tracking_available: true });
      useSessionPullRequestsMock.mockReturnValue({
        data: {
          synced: true,
          prs: [
            {
              url: "updated",
              number: 1,
              state: "open",
              is_draft: storedDraft,
              state_checked_at: 3,
            },
          ],
        },
      });
      usePullRequestInfosMock.mockReturnValue([
        { dataUpdatedAt: 2000, data: { pr: { url: "updated", state: "OPEN", ...live } } },
      ]);
      expect(run().result.current.prStatus).toBe(expected);
    },
  );

  it("preserves the legacy single PR's full status without another lookup", () => {
    setWorktrees(undefined);
    setGithub({
      repo: { name_with_owner: "org/repo" },
      pr: {
        url: "https://github.com/org/repo/pull/6000",
        number: 6000,
        title: "Legacy host PR",
        state: "OPEN",
        merge_state_status: "CLEAN",
      },
    });
    expect(run().result.current).toMatchObject({
      prCount: 1,
      prNumber: 6000,
      prStatus: "ready",
      prs: [{ number: 6000, repository: "org/repo", title: "Legacy host PR" }],
    });
    expect(usePullRequestInfosMock).toHaveBeenCalledWith("conv-1", []);
  });

  it.each([
    { live: "the host is offline", query: { isError: true, isFetching: false } },
    { live: "live info still loads", query: { isError: false, isLoading: true, isFetching: true } },
  ])("names the server's stored PRs while $live", ({ query }) => {
    setWorktrees(undefined);
    usePullRequestInfoMock.mockReturnValue({ data: undefined, refetch: vi.fn(), ...query });
    useSessionPullRequestsMock.mockReturnValue({
      data: {
        prs: [
          { url: "merged", number: 1, state: "MERGED" },
          { url: "https://github.com/o/r/pull/42", number: 42, provider: "github", state: "OPEN" },
        ],
        synced: true,
      },
    });
    expect(run().result.current).toMatchObject({
      githubState: "ready",
      prCount: 2,
      prNumber: 42,
      prStatus: "open",
    });
  });

  it("keeps a live PR from a host without tracking over the empty stored list", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ branch: "main" })] });
    setGithub({ pr: { number: 6000 } });
    useSessionPullRequestsMock.mockReturnValue({ data: { prs: [], synced: true } });
    const { result } = run();
    expect(result.current.prCount).toBe(1);
    expect(result.current.prNumber).toBe(6000);
  });

  it("styles the PR number by the primary PR's provider, then the session's", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ branch: "main" })] });
    const pr = (over: Record<string, unknown> = {}) => ({
      url: "u",
      host: "forge.example.test",
      repository: "org/project/repo",
      number: 7,
      relationship: "created",
      ...over,
    });
    const prefix = () => run().result.current.prNumberPrefix;

    // The PR's own provider wins over the session's.
    setGithub({
      provider: "github",
      prs: [pr({ provider: "example_forge", provider_display: FORGE_DISPLAY })],
    });
    expect(prefix()).toBe("!");
    setGithub({
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      prs: [pr({ provider: "github" })],
    });
    expect(prefix()).toBe("#");
    // Without one, the session's provider decides, for tracked and untracked PRs.
    setGithub({ provider: "example_forge", provider_display: FORGE_DISPLAY, prs: [pr()] });
    expect(prefix()).toBe("!");
    setGithub({ provider: "example_forge", provider_display: FORGE_DISPLAY, pr: { number: 7 } });
    expect(prefix()).toBe("!");
    expect(run().result.current.prNumber).toBe(7);
  });

  it("uses the # prefix when no provider marks the PR", () => {
    setWorktrees({ status: "ok", worktrees: [wt({ branch: "main" })] });
    const prefix = () => run().result.current.prNumberPrefix;
    // A host that predates the provider field serves GitHub.
    setGithub({ prs: [{ url: "u", host: "github.com", repository: "o/r", number: 7 }] });
    expect(prefix()).toBe("#");
    // No provider serves the workspace, and there is nothing loaded yet.
    setGithub({ provider: null, pr: { number: 7 } });
    expect(prefix()).toBe("#");
    setGithub(undefined);
    expect(prefix()).toBe("#");
  });

  it("refresh() refetches both sources and surfaces fetching", () => {
    const wtRefetch = vi.fn();
    const ghRefetch = vi.fn();
    const detailRefetch = vi.fn();
    usePullRequestInfosMock.mockReturnValue([{ refetch: detailRefetch, isFetching: true }]);
    setWorktrees(
      { status: "ok", worktrees: [wt({ branch: "main" })] },
      { isFetching: true, refetch: wtRefetch },
    );
    usePullRequestInfoMock.mockReturnValue({
      data: undefined,
      isFetching: false,
      refetch: ghRefetch,
    });
    const { result, rerender } = run();
    expect(result.current.refreshing).toBe(true);
    const refresh = result.current.refresh;
    const latestDetailRefetch = vi.fn();
    usePullRequestInfosMock.mockReturnValue([{ refetch: latestDetailRefetch }]);
    rerender();
    expect(result.current.refresh).toBe(refresh);
    refresh();
    expect(wtRefetch).toHaveBeenCalledTimes(1);
    expect(ghRefetch).toHaveBeenCalledTimes(1);
    expect(detailRefetch).not.toHaveBeenCalled();
    expect(latestDetailRefetch).toHaveBeenCalledTimes(1);
  });
});
