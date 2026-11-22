/**
 * Split a server-created worktree path into the repo it came from and
 * the worktree's directory name, or null when the path doesn't look
 * like one. The host creates worktrees as
 * ``<parent>/<repo>-worktrees/<branch-dir>`` siblings of the repo
 * (``branch-dir`` is the sanitized branch name), so
 * ``/Users/a/proj-worktrees/fix`` → repo ``/Users/a/proj``,
 * branchDir ``fix``.
 */
export function splitWorktreePath(workspace: string): { repo: string; branchDir: string } | null {
  const slash = workspace.lastIndexOf("/");
  if (slash <= 0 || slash === workspace.length - 1) return null;
  const parent = workspace.slice(0, slash);
  if (!parent.endsWith("-worktrees")) return null;
  const repo = parent.slice(0, -"-worktrees".length);
  if (!repo.includes("/") || repo.endsWith("/")) return null;
  return { repo, branchDir: workspace.slice(slash + 1) };
}
