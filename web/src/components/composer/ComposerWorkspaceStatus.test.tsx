import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerWorkspaceStatus } from "./ComposerWorkspaceStatus";

afterEach(cleanup);

const base = {
  workspacePath: "/home/alice/repo",
  worktreePath: "/home/alice/repo",
  isWorktree: false,
  branch: "feature/login",
  branchState: "branch" as const,
  creationBranch: null,
};

function openBranch() {
  fireEvent.pointerDown(screen.getByTestId("composer-git-branch"), { button: 0 });
}

describe("ComposerWorkspaceStatus", () => {
  it("labels the directory by its trailing segment and the branch by name", () => {
    render(<ComposerWorkspaceStatus {...base} />);
    expect(screen.getByTestId("composer-workspace-dir")).toHaveTextContent("repo");
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("feature/login");
  });

  it("heads the branch popover 'Git branch', never 'Session worktree' (#7067)", () => {
    render(<ComposerWorkspaceStatus {...base} />);
    openBranch();
    expect(screen.getByText("Git branch")).toBeInTheDocument();
    expect(screen.queryByText(/Session worktree/i)).toBeNull();
  });

  it("heads the directory popover 'Worktree' for a linked worktree", () => {
    render(
      <ComposerWorkspaceStatus
        {...base}
        isWorktree
        workspacePath="/home/alice/repo-wt/feat"
        worktreePath="/home/alice/repo-wt/feat"
      />,
    );
    fireEvent.pointerDown(screen.getByTestId("composer-workspace-dir"), { button: 0 });
    expect(screen.getByText("Worktree")).toBeInTheDocument();
    expect(screen.getByText(/A linked git worktree/i)).toBeInTheDocument();
  });

  it("models detached HEAD honestly", () => {
    render(<ComposerWorkspaceStatus {...base} branch={null} branchState="detached" />);
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("Detached HEAD");
    openBranch();
    expect(screen.getByText(/no branch is checked out/i)).toBeInTheDocument();
  });

  it("distinguishes non-git, unavailable, and loading states", () => {
    const { rerender } = render(
      <ComposerWorkspaceStatus {...base} branch={null} branchState="not-git" />,
    );
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("Not a Git repository");
    rerender(<ComposerWorkspaceStatus {...base} branch={null} branchState="unknown" />);
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("Branch unavailable");
    rerender(<ComposerWorkspaceStatus {...base} branch={null} branchState="loading" />);
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("Checking branch…");
  });

  it("surfaces the creation-time branch separately, never as the live label", () => {
    render(
      <ComposerWorkspaceStatus
        {...base}
        branch={null}
        branchState="unknown"
        creationBranch="feature/created"
      />,
    );
    // Live label stays honest…
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("Branch unavailable");
    // …and the creation branch is history in the popover, clearly labelled.
    openBranch();
    expect(screen.getByText(/Created on branch feature\/created/i)).toBeInTheDocument();
  });

  it("does not repeat the creation branch when it equals the live branch", () => {
    render(
      <ComposerWorkspaceStatus {...base} branch="feature/login" creationBranch="feature/login" />,
    );
    openBranch();
    expect(screen.queryByText(/Created on branch/i)).toBeNull();
  });

  it("invokes the refresh callback without closing the popover", () => {
    const onRefreshBranch = vi.fn();
    render(<ComposerWorkspaceStatus {...base} onRefreshBranch={onRefreshBranch} />);
    openBranch();
    fireEvent.click(screen.getByTestId("composer-git-branch-refresh"));
    expect(onRefreshBranch).toHaveBeenCalledTimes(1);
  });

  it("hides the refresh button when no callback is supplied", () => {
    render(<ComposerWorkspaceStatus {...base} />);
    openBranch();
    expect(screen.queryByTestId("composer-git-branch-refresh")).toBeNull();
  });
});
