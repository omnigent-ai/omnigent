import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HostWorktree } from "@/hooks/useHostWorktrees";
import type { SessionWorktreesResult } from "@/hooks/useSessionWorktrees";
import { useComposerGitStatus } from "./useComposerGitStatus";

const useSessionWorktreesMock = vi.fn();
const useGithubInfoMock = vi.fn();

vi.mock("@/hooks/useSessionWorktrees", () => ({
  useSessionWorktrees: (
    sessionId: string | null,
    hostId: string | null,
    workspace: string | null,
  ) => useSessionWorktreesMock(sessionId, hostId, workspace),
}));
vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: (id: string | undefined) => useGithubInfoMock(id),
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
function setGithub(data: Record<string, unknown> | undefined) {
  useGithubInfoMock.mockReturnValue({ data, isFetching: false, refetch: vi.fn() });
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
  useGithubInfoMock.mockReset();
});

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

  it("refresh() refetches both sources and surfaces fetching", () => {
    const wtRefetch = vi.fn();
    const ghRefetch = vi.fn();
    setWorktrees(
      { status: "ok", worktrees: [wt({ branch: "main" })] },
      { isFetching: true, refetch: wtRefetch },
    );
    useGithubInfoMock.mockReturnValue({ data: undefined, isFetching: false, refetch: ghRefetch });
    const { result } = run();
    expect(result.current.refreshing).toBe(true);
    result.current.refresh();
    expect(wtRefetch).toHaveBeenCalledTimes(1);
    expect(ghRefetch).toHaveBeenCalledTimes(1);
  });
});
