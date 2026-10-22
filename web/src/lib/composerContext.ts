export type WorkingDirectorySelection = { kind: "unset" } | { kind: "selected"; path: string };

export type WorktreeSelection =
  | { kind: "none" }
  | { kind: "existing"; path: string; branch: string }
  | { kind: "new"; branchName: string; baseBranch: string | null };

export interface ComposerContextState {
  workingDirectory: WorkingDirectorySelection;
  worktree: WorktreeSelection;
}

export type WorkingDirectoryGitState = "unknown" | "git" | "not_git";

export const EMPTY_COMPOSER_CONTEXT: ComposerContextState = {
  workingDirectory: { kind: "unset" },
  worktree: { kind: "none" },
};

export function isAbsoluteComposerPath(path: string): boolean {
  return path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("\\\\");
}

function normalizeWorkingDirectory(
  selection: WorkingDirectorySelection,
): WorkingDirectorySelection {
  if (selection.kind === "unset") return selection;
  const path = selection.path.trim();
  return path !== "" && isAbsoluteComposerPath(path)
    ? { kind: "selected", path }
    : { kind: "unset" };
}

function normalizeWorktree(selection: WorktreeSelection): WorktreeSelection {
  if (selection.kind === "none") return selection;
  if (selection.kind === "existing") {
    const path = selection.path.trim();
    const branch = selection.branch.trim();
    if (path === "" || !isAbsoluteComposerPath(path) || branch === "") return { kind: "none" };
    return { kind: "existing", path, branch };
  }
  const branchName = selection.branchName.trim();
  if (branchName === "") return { kind: "none" };
  return { kind: "new", branchName, baseBranch: selection.baseBranch?.trim() || null };
}

export function normalizeComposerContextState(
  state: ComposerContextState,
  workingDirectoryGitState: WorkingDirectoryGitState = "unknown",
): ComposerContextState {
  const workingDirectory = normalizeWorkingDirectory(state.workingDirectory);
  let worktree = normalizeWorktree(state.worktree);
  if (workingDirectoryGitState === "not_git") worktree = { kind: "none" };

  return {
    workingDirectory,
    worktree,
  };
}
