// What the session's working-copy diff compares against. The runner diffs a Git
// workspace against HEAD; elsewhere it diffs against copies it recorded, so the
// review names HEAD only when the workspace is known to be a Git worktree.

import type { DiffReviewBase } from "@/components/diff-review/DiffReviewBaseMenu";
import { matchWorktree } from "@/hooks/useHostWorktrees";
import { branchPointLabel, type EffectiveComparison } from "@/lib/workingCopyComparison";
import type { WorkingCopyBaseCommand } from "@/lib/workingCopyBaseCommands";
import { useSessionWorktrees, type SessionWorktreesResult } from "@/hooks/useSessionWorktrees";
import { useWorkspacePaths } from "./FileViewerContext";
import { useSessionHostId } from "./fileCommands/target";

export interface WorkingCopyBase {
  /** Short label for the review's toolbar, such as "vs HEAD". */
  label: string;
  /** A longer description for the label's tooltip. */
  description: string;
}

/** The base a workspace's working-copy diff uses, or null when it isn't known. */
export function workingCopyBase(
  worktrees: SessionWorktreesResult | undefined,
  workspace: string | null,
): WorkingCopyBase | null {
  if (worktrees?.status !== "ok" || !workspace) return null;
  const worktree = matchWorktree(worktrees.worktrees, workspace);
  if (!worktree) return null;
  const branch = worktree.detached ? null : worktree.branch;
  return {
    label: "vs HEAD",
    description: branch
      ? `Uncommitted and untracked changes compared with the last commit on ${branch}.`
      : "Uncommitted and untracked changes compared with the last commit.",
  };
}

/** The base of the session's working-copy diff, from the cached worktree probe. */
export function useWorkingCopyBase(conversationId: string): WorkingCopyBase | null {
  const hostId = useSessionHostId();
  const { root } = useWorkspacePaths();
  const worktrees = useSessionWorktrees(conversationId, hostId, root);
  return workingCopyBase(worktrees.data, root);
}

/** The last commit, named without a worktree probe. */
export const LAST_COMMIT_BASE: WorkingCopyBase = {
  label: "vs HEAD",
  description: "Uncommitted and untracked changes compared with the last commit.",
};

/** The toolbar's base: the label and, where the source can compare either way, the menu. */
export function workingCopyBaseMenu(
  comparison: EffectiveComparison,
  headBase: WorkingCopyBase | null,
  commands: readonly WorkingCopyBaseCommand[],
): DiffReviewBase | null {
  const shown =
    comparison.base === "branch-point"
      ? branchPointLabel(comparison.comparison)
      : comparison.base === "head"
        ? headBase
        : // Nothing is listed yet, or the chosen branch point can't be made: name what was asked for.
          { label: "vs branch point", description: comparison.message };
  if (!shown) return null;
  return {
    ...shown,
    choices: commands
      .filter((command) => command.checked !== undefined)
      .map((command) => ({
        id: command.id,
        label: command.label,
        checked: command.checked === true,
        unavailableReason: command.unavailableReason,
        onSelect: command.run,
      })),
    actions: commands
      .filter((command) => command.checked === undefined)
      .map((command) => ({ id: command.id, label: command.label, onSelect: command.run })),
  };
}
