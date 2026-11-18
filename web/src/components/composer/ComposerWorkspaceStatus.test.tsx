import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { ComposerWorkspaceStatus } from "./ComposerWorkspaceStatus";

afterEach(cleanup);

// The chips' own provider opens tooltips after 600ms; leave room on slow runners.
const TOOLTIP_WAIT = { timeout: 3000 };

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

  it("shows the full working directory path in a hover tooltip", async () => {
    const user = userEvent.setup();
    render(<ComposerWorkspaceStatus {...base} workspacePath="/home/alice/projects/repo" />);
    const directory = screen.getByTestId("composer-workspace-dir");
    expect(directory).not.toHaveAttribute("title");
    // The name already carries the path, so no duplicate description.
    expect(directory).not.toHaveAttribute("aria-describedby");
    await user.hover(directory);
    expect(await screen.findByRole("tooltip", {}, TOOLTIP_WAIT)).toHaveTextContent(
      "Working directory: /home/alice/projects/repo",
    );
  });

  it("keeps creation-time branch history in the read-only tooltip, never the live label", async () => {
    const user = userEvent.setup();
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
    expect(worktree).not.toHaveAttribute("title");
    // Screen readers get the history without hovering.
    expect(worktree).toHaveAccessibleDescription(
      "Branch unavailable. Created on branch feature/created.",
    );
    await user.hover(worktree);
    expect(await screen.findByRole("tooltip", {}, TOOLTIP_WAIT)).toHaveTextContent(
      "Branch unavailable. Created on branch feature/created.",
    );
  });
});
