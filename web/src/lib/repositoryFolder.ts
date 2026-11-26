// Where a Diffs review's repository sits in a session's workspace: the worktree holding the
// folder the session browses, its name as the workspace API takes it, and paths between them.

import {
  type HostWorktree,
  isWorktreeRoot,
  matchWorktree,
  pathIsWithinWorktree,
} from "@/hooks/useHostWorktrees";

function trimTrailingSlashes(path: string): string {
  return path.replace(/(.)[/\\]+$/, "$1");
}

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
  return trimTrailingSlashes(holder.path);
}

/**
 * `root` as the workspace API names it: a folder relative to the workspace, empty for the
 * workspace itself, or null when it lies outside the workspace, where Diffs can't review it.
 */
export function repositoryFolderOf(root: string, workspace: string): string | null {
  const base = trimTrailingSlashes(workspace);
  const folder = trimTrailingSlashes(root);
  if (folder === base) return "";
  if (!pathIsWithinWorktree(folder, base)) return null;
  return folder.slice(base.length + 1);
}

/** A file listed in `repository`, as the workspace names it. */
export function workspacePathInRepository(path: string, repository: string): string {
  return repository ? `${repository}/${path}` : path;
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
  let relative = path;
  if (path.startsWith("/")) {
    if (!workspace) return null;
    const inWorkspace = repositoryFolderOf(path, workspace);
    if (inWorkspace === null) return null;
    relative = inWorkspace;
  }
  if (!repository) return relative;
  return relative.startsWith(`${repository}/`) ? relative.slice(repository.length + 1) : null;
}
