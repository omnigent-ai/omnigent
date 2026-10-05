import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ComposerWorkspaceStatus } from "./ComposerWorkspaceStatus";

afterEach(cleanup);

const base = {
  workspacePath: "/home/alice/repo",
  sandboxRepos: [] as string[],
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
    expect(directory).toHaveAccessibleName("Working directory: /home/alice/repo");
    expect(worktree).toHaveAccessibleName("Worktree: feature/login");
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

  it("names the sandbox repository while the launch has not bound a workspace", () => {
    render(
      <ComposerWorkspaceStatus
        {...base}
        workspacePath={null}
        sandboxRepos={["https://github.com/org/fixture-repo.git"]}
        showWorktree={false}
      />,
    );
    const directory = screen.getByTestId("composer-workspace-dir");
    expect(directory).toHaveTextContent("fixture-repo");
    expect(directory).toHaveAccessibleName(
      "Sandbox repository: https://github.com/org/fixture-repo.git",
    );
    expect(directory).toHaveAttribute(
      "title",
      "Sandbox repository: https://github.com/org/fixture-repo.git",
    );
  });

  it("keeps the repository's branch in the label and counts several repositories", () => {
    const { rerender } = render(
      <ComposerWorkspaceStatus
        {...base}
        workspacePath={null}
        sandboxRepos={["git@github.com:org/api.git#release"]}
        showWorktree={false}
      />,
    );
    expect(screen.getByTestId("composer-workspace-dir")).toHaveTextContent("api#release");

    rerender(
      <ComposerWorkspaceStatus
        {...base}
        workspacePath={null}
        sandboxRepos={["https://github.com/org/api", "https://github.com/org/web#main"]}
        showWorktree={false}
      />,
    );
    const directory = screen.getByTestId("composer-workspace-dir");
    expect(directory).toHaveTextContent("2 repositories");
    expect(directory).toHaveAccessibleName(
      "Sandbox repositories: https://github.com/org/api, https://github.com/org/web#main",
    );
  });

  it("prefers the bound working directory over the sandbox repositories", () => {
    render(
      <ComposerWorkspaceStatus {...base} sandboxRepos={["https://github.com/org/other.git"]} />,
    );
    expect(screen.getByTestId("composer-workspace-dir")).toHaveAccessibleName(
      "Working directory: /home/alice/repo",
    );
  });

  it("reads 'No workspace' only when neither a directory nor a repository is known", () => {
    render(
      <ComposerWorkspaceStatus
        {...base}
        workspacePath={null}
        sandboxRepos={[]}
        showWorktree={false}
      />,
    );
    const directory = screen.getByTestId("composer-workspace-dir");
    expect(directory).toHaveTextContent("No workspace");
    expect(directory).toHaveAccessibleName("Working directory: Not selected");
  });
});
