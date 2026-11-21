import { describe, expect, it } from "vitest";
import type { HostWorktree } from "@/hooks/useHostWorktrees";
import type { WorkingCopyBaseCommand } from "@/lib/workingCopyBaseCommands";
import { LAST_COMMIT_BASE, workingCopyBase, workingCopyBaseMenu } from "./workingCopyBase";

function worktree(path: string, branch: string | null): HostWorktree {
  return { path, branch, detached: branch === null } as HostWorktree;
}

describe("workingCopyBase", () => {
  it("names HEAD and the branch for a workspace inside a Git worktree", () => {
    const base = workingCopyBase(
      { status: "ok", worktrees: [worktree("/repo", "main"), worktree("/repo-wt", "feature")] },
      "/repo-wt/web",
    );
    expect(base).toEqual({
      label: "vs HEAD",
      description: "Uncommitted and untracked changes compared with the last commit on feature.",
    });
  });

  it("leaves the branch out for a detached HEAD", () => {
    expect(
      workingCopyBase({ status: "ok", worktrees: [worktree("/repo", null)] }, "/repo")?.description,
    ).toBe("Uncommitted and untracked changes compared with the last commit.");
  });

  it("makes no claim unless the workspace is known to be in a Git worktree", () => {
    expect(workingCopyBase(undefined, "/repo")).toBeNull();
    expect(workingCopyBase({ status: "not_git" }, "/repo")).toBeNull();
    expect(workingCopyBase({ status: "unknown" }, "/repo")).toBeNull();
    expect(
      workingCopyBase({ status: "ok", worktrees: [worktree("/other", "main")] }, "/repo"),
    ).toBeNull();
    expect(
      workingCopyBase({ status: "ok", worktrees: [worktree("/repo", "main")] }, null),
    ).toBeNull();
  });
});

describe("workingCopyBaseMenu", () => {
  const commands: WorkingCopyBaseCommand[] = [
    {
      id: "compare-automatic",
      label: "Automatic",
      name: "Automatic",
      checked: false,
      unavailableReason: null,
      run: () => {},
    },
    {
      id: "compare-branch-point",
      label: "All Branch Changes",
      name: "All branch changes",
      checked: true,
      unavailableReason: null,
      run: () => {},
    },
    {
      id: "refresh-changes",
      label: "Refresh Changes",
      name: "Refresh changes",
      unavailableReason: null,
      run: () => {},
    },
  ];

  it("names the base shown, with the choices and Refresh in its menu", () => {
    const menu = workingCopyBaseMenu(
      { base: "head", note: null, override: false, retry: false },
      LAST_COMMIT_BASE,
      commands,
    );
    expect(menu).toMatchObject({
      label: "vs HEAD",
      choices: [
        { id: "compare-automatic", checked: false },
        { id: "compare-branch-point", checked: true },
      ],
      actions: [{ id: "refresh-changes", label: "Refresh Changes" }],
    });
  });

  it("names the branch point while it's coming or can't be made, never the last commit", () => {
    for (const comparison of [
      { base: "pending" as const, message: "Comparing with the branch point…" },
      { base: "unavailable" as const, message: "No branch point with origin/main.", retry: false },
    ]) {
      expect(workingCopyBaseMenu(comparison, LAST_COMMIT_BASE, commands)).toMatchObject({
        label: "vs branch point",
        description: comparison.message,
      });
    }
  });

  it("has no menu for a last-commit base it can't name", () => {
    expect(
      workingCopyBaseMenu(
        { base: "head", note: null, override: false, retry: false },
        null,
        commands,
      ),
    ).toBeNull();
  });
});
