// Resolves what the session header shows as "where am I working": the
// repository (or plain folder) the session's workspace points at, and the
// branch/worktree checked out in it.
//
// The runner's environment metadata is the accurate source — it reads the
// workspace's real ``.git`` state. The session snapshot is the fallback for
// when that metadata is missing (runner offline, host-served workspace): it
// still carries the workspace path, plus the branch when the server created a
// worktree for the session.

import type { WorkspaceGitIdentity } from "@/hooks/useWorkspaceChangedFiles";

/** What the header renders for the session's working location. */
export interface WorkspaceIdentity {
  /** Repository name, or the workspace folder name outside a repo. */
  name: string;
  /** Branch name, or the short commit when {@link detached}. */
  ref: string | null;
  /** Whether {@link ref} is a commit rather than a branch. */
  detached: boolean;
  /** Whether the checkout is a linked worktree of {@link name}. */
  worktree: boolean;
  /** Whether {@link name} is a git repository rather than a plain folder. */
  isRepo: boolean;
}

/**
 * Take the last segment of an absolute path, tolerating either separator and
 * trailing slashes, e.g. ``"/Users/alice/myrepo/"`` → ``"myrepo"``.
 */
function basename(path: string): string {
  const segments = path.split(/[/\\]/).filter((segment) => segment !== "");
  return segments[segments.length - 1] ?? "";
}

/**
 * Resolve the session's working location from the best source available.
 *
 * @param git - Git identity from the runner's environment metadata, or null
 *   when the workspace isn't a repo (and undefined while the probe is in
 *   flight or the runner is unreachable).
 * @param envRoot - The environment's reported root. Preferred over
 *   ``workspacePath`` outside a repo: the two disagree when the runner starts
 *   the session somewhere other than its bound workspace, and the root is
 *   where the agent actually works.
 * @param workspacePath - The session's bound workspace directory.
 * @param sessionBranch - ``Session.gitBranch`` — set only when the server
 *   created a worktree for this session, so it implies one.
 * @returns The identity to render, or null when nothing is known yet.
 */
export function deriveWorkspaceIdentity(
  git: WorkspaceGitIdentity | null | undefined,
  envRoot: string | null | undefined,
  workspacePath: string | null | undefined,
  sessionBranch: string | null | undefined,
): WorkspaceIdentity | null {
  const branch = sessionBranch?.trim() ? sessionBranch : null;
  if (git) {
    return {
      name: git.repo,
      ref: git.ref ?? branch,
      // A ref that fell back to the session's branch is a branch, not a commit.
      detached: git.ref !== null && git.detached,
      worktree: git.worktree || branch !== null,
      isRepo: true,
    };
  }
  const path = envRoot?.trim() ? envRoot : workspacePath;
  const name = path?.trim() ? basename(path) : "";
  if (name === "") return null;
  return {
    name,
    ref: branch,
    detached: false,
    // The snapshot only reports a branch for a server-created worktree.
    worktree: branch !== null,
    isRepo: branch !== null,
  };
}

/**
 * Screen-reader label for an identity, e.g.
 * ``"Repository omnigent, worktree on branch feature/login"``.
 */
export function describeWorkspaceIdentity(identity: WorkspaceIdentity): string {
  const subject = identity.isRepo ? `Repository ${identity.name}` : `Folder ${identity.name}`;
  if (identity.ref === null) return subject;
  const checkout = identity.worktree ? "worktree" : "checkout";
  const at = identity.detached ? `detached at ${identity.ref}` : `on branch ${identity.ref}`;
  return `${subject}, ${checkout} ${at}`;
}
