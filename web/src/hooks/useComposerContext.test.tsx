import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useComposerContext } from "./useComposerContext";

describe("useComposerContext", () => {
  it("owns controlled updates for all context categories", () => {
    const onChange = vi.fn();
    const { result } = renderHook(() => useComposerContext({ onChange }));

    act(() => {
      result.current.setWorkingDirectory({ kind: "selected", path: "/repo" });
      result.current.setWorktree({ kind: "new", branchName: "feature", baseBranch: null });
      result.current.setRepositories([
        { id: "repo", url: "git@example.com:repo.git", branch: null },
      ]);
      result.current.setMcpContext([{ id: "github", serverName: "github" }]);
    });

    expect(result.current.state).toEqual({
      workingDirectory: { kind: "selected", path: "/repo" },
      worktree: { kind: "new", branchName: "feature", baseBranch: null },
      repositories: [{ id: "repo", url: "git@example.com:repo.git", branch: null }],
      mcpContext: [{ id: "github", serverName: "github" }],
    });
    expect(onChange).toHaveBeenLastCalledWith(result.current.state);
  });

  it("drops an incompatible worktree when the directory becomes known non-Git", () => {
    const { result, rerender } = renderHook(
      ({ gitState }: { gitState: "unknown" | "not_git" }) =>
        useComposerContext({
          initialState: {
            workingDirectory: { kind: "selected", path: "/repo" },
            worktree: { kind: "new", branchName: "feature", baseBranch: null },
            repositories: [],
            mcpContext: [],
          },
          workingDirectoryGitState: gitState,
        }),
      { initialProps: { gitState: "unknown" } },
    );

    rerender({ gitState: "not_git" });
    expect(result.current.state.worktree).toEqual({ kind: "none" });
  });
});
