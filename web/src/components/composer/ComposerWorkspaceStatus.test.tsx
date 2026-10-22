import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerWorkspaceStatus } from "./ComposerWorkspaceStatus";

const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));

afterEach(() => {
  cleanup();
  copyTextMock.mockReset();
  vi.useRealTimers();
});

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
  it("renders the working directory as a copy button and the worktree as read-only text", () => {
    render(<ComposerWorkspaceStatus {...base} />);
    const directory = screen.getByTestId("composer-workspace-dir");
    const worktree = screen.getByTestId("composer-git-branch");
    expect(directory).toHaveTextContent("repo");
    expect(worktree).toHaveTextContent("feature/login");
    expect(directory).toHaveAccessibleName("Copy working directory: /home/alice/repo");
    expect(worktree).toHaveAccessibleName("Worktree: feature/login");
    expect(directory.tagName).toBe("BUTTON");
    expect(worktree.tagName).toBe("SPAN");
    expect(directory).toHaveClass("text-muted-foreground");
    expect(worktree).toHaveClass("text-muted-foreground");
    expect(screen.getAllByRole("button")).toEqual([directory]);
  });

  it("copies the full working directory path and confirms in place", async () => {
    vi.useFakeTimers();
    copyTextMock.mockResolvedValue(undefined);
    render(<ComposerWorkspaceStatus {...base} />);
    const directory = screen.getByTestId("composer-workspace-dir");
    await act(async () => {
      fireEvent.click(directory);
    });
    expect(copyTextMock).toHaveBeenCalledWith("/home/alice/repo");
    expect(directory).toHaveAccessibleDescription("Copied: /home/alice/repo");
    expect(directory).toHaveClass("text-green-600");
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(directory).not.toHaveAttribute("aria-describedby");
    expect(directory).toHaveClass("text-muted-foreground");
  });

  it("reports a failed copy on the chip", async () => {
    copyTextMock.mockRejectedValue(new Error("Clipboard API not available"));
    render(<ComposerWorkspaceStatus {...base} />);
    const directory = screen.getByTestId("composer-workspace-dir");
    await act(async () => {
      fireEvent.click(directory);
    });
    expect(directory).toHaveAccessibleDescription("Copy failed");
    expect(directory).toHaveClass("text-destructive");
  });

  it("keeps an unbound working directory read-only", () => {
    render(<ComposerWorkspaceStatus {...base} workspacePath={null} />);
    const directory = screen.getByTestId("composer-workspace-dir");
    expect(directory.tagName).toBe("SPAN");
    expect(directory).toHaveAccessibleName("Working directory: Not selected");
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
      "Copy working directory: /home/alice/projects/repo",
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
