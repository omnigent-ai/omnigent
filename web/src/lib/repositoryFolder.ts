// Where a Diffs review's repository sits in a session's workspace: the worktree holding the
// folder the session browses, its name as the workspace API takes it, and paths between them.

import {
  type HostWorktree,
  isAbsoluteHostPath,
  isHostRoot,
  isWindowsHostPath,
  isWorktreeRoot,
  matchWorktree,
  pathInWorktree,
  trimHostPath,
} from "@/hooks/useHostWorktrees";

/**
 * Whether `workspace` is a folder inside a larger repository rather than a repository's own
 * root, where the host compares only the workspace and refuses every folder in it.
 */
export function workspaceInsideLargerRepository(
  workspace: string,
  worktrees: HostWorktree[],
): boolean {
  const own = matchWorktree(worktrees, workspace);
  return own !== null && !isWorktreeRoot(own.path, workspace);
}

/**
 * The repository root Diffs reviews for a session's `directory`: the most specific worktree
 * holding it, or the workspace when that is the workspace's own worktree, nothing holds it,
 * or the workspace lies inside a larger repository.
 */
export function diffRootFor(
  directory: string | null,
  workspace: string,
  worktrees: HostWorktree[],
): string {
  if (!directory || workspaceInsideLargerRepository(workspace, worktrees)) return workspace;
  const holder = matchWorktree(worktrees, directory);
  if (!holder || holder === matchWorktree(worktrees, workspace)) return workspace;
  return trimHostPath(holder.path);
}

/**
 * `root` as the workspace API names it: a folder relative to the workspace, empty for the
 * workspace itself, or null when it lies outside the workspace, where Diffs can't review it.
 */
export function repositoryFolderOf(root: string, workspace: string): string | null {
  return pathInWorktree(workspace, root);
}

/** A file listed in `repository`, as the workspace names it. */
export function workspacePathInRepository(path: string, repository: string): string {
  return repository ? `${repository}/${path}` : path;
}

/** `relative` under `workspace`, adding no second separator after a root such as `/` or `C:\`. */
function inWorkspace(workspace: string, relative: string): string {
  const base = trimHostPath(workspace);
  return isHostRoot(base) ? `${base}${relative}` : `${base}/${relative}`;
}

/** `repository`'s root as an absolute path, as the workspace's host names it; the workspace's own for none. */
export function repositoryRootIn(workspace: string, repository: string): string {
  return repository ? inWorkspace(workspace, repository) : workspace;
}

/**
 * A workspace-relative or absolute path as `repository`'s listing names it; null when it lies
 * outside that folder.
 */
export function repositoryPathOf(
  path: string,
  repository: string,
  workspace: string | null,
): string | null {
  // On a POSIX workspace `C:\a.ts` is a file name, so only a known Windows workspace reads drives.
  const absolute =
    workspace && !isWindowsHostPath(workspace) ? path.startsWith("/") : isAbsoluteHostPath(path);
  if (!absolute && !repository) return path;
  if (!workspace) {
    if (absolute) return null;
    return path.startsWith(`${repository}/`) ? path.slice(repository.length + 1) : null;
  }
  // Matched on the host's terms, so a Windows path's separators and case don't matter.
  const inRepository = pathInWorktree(
    repository ? inWorkspace(workspace, repository) : workspace,
    absolute ? path : inWorkspace(workspace, path),
  );
  return repository && inRepository === "" ? null : inRepository;
}
