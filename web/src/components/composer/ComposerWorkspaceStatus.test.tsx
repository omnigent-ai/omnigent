import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ComposerWorkspaceStatus } from "./ComposerWorkspaceStatus";

afterEach(cleanup);

const base = {
  workspacePath: "/home/alice/repo",
  worktreePath: "/home/alice/repo",
  isWorktree: false,
  branch: "feature/login",
  branchState: "branch" as const,
  creationBranch: null,
  showWorktree: true,
};

describe("ComposerWorkspaceStatus", () => {
  it("renders the working directory and selected worktree as read-only secondary text", () => {
    render(<ComposerWorkspaceStatus {...base} />);
    const directory = screen.getByTestId("composer-workspace-dir");
    const worktree = screen.getByTestId("composer-git-branch");
    expect(directory).toHaveTextContent("repo");
    expect(worktree).toHaveTextContent("feature/login");
    expect(directory.tagName).toBe("SPAN");
    expect(worktree.tagName).toBe("SPAN");
    expect(directory).toHaveClass("text-muted-foreground");
    expect(worktree).toHaveClass("text-muted-foreground");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("completely hides worktree information for folders not confirmed as GitHub repositories", () => {
    render(<ComposerWorkspaceStatus {...base} showWorktree={false} />);
    expect(screen.getByTestId("composer-workspace-dir")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-git-branch")).toBeNull();
  });

  it("models detached, non-git, unavailable, and loading states honestly", () => {
    const { rerender } = render(
      <ComposerWorkspaceStatus {...base} branch={null} branchState="detached" />,
    );
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("Detached HEAD");
    rerender(<ComposerWorkspaceStatus {...base} branch={null} branchState="not-git" />);
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("Not a Git repository");
    rerender(<ComposerWorkspaceStatus {...base} branch={null} branchState="unknown" />);
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("Branch unavailable");
    rerender(<ComposerWorkspaceStatus {...base} branch={null} branchState="loading" />);
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("Checking branch…");
  });

  it("keeps creation-time branch history in the read-only title, never the live label", () => {
    render(
      <ComposerWorkspaceStatus
        {...base}
        branch={null}
        branchState="unknown"
        creationBranch="feature/created"
      />,
    );
    const worktree = screen.getByTestId("composer-git-branch");
    expect(worktree).toHaveTextContent("Branch unavailable");
    expect(worktree).toHaveAttribute(
      "title",
      "Branch unavailable. Created on branch feature/created.",
    );
  });
});
