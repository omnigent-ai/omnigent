import { useCallback } from "react";

import { useGithubInfo } from "@/hooks/useGithub";
import type { HostWorktree } from "@/hooks/useHostWorktrees";
import { useSessionWorktrees } from "@/hooks/useSessionWorktrees";

/** Trailing-slash-insensitive path key. */
function normalizePath(path: string): string {
  return path.replace(/[\\/]+$/, "");
}

/**
 * The worktree whose root contains ``workspace`` — exact match, else the
 * deepest root ``workspace`` sits under (a workspace bound to a subdir of a
 * worktree). ``null`` when nothing contains it.
 */
function matchWorktree(worktrees: HostWorktree[], workspace: string): HostWorktree | null {
  const ws = normalizePath(workspace);
  let best: HostWorktree | null = null;
  for (const wt of worktrees) {
    const root = normalizePath(wt.path);
    if (ws === root) return wt;
    if (ws.startsWith(`${root}/`) && (best === null || root.length > best.path.length)) {
      best = wt;
    }
  }
  return best;
}

/** How much we actually know about the session's checked-out branch. */
export type ComposerBranchState =
  | "loading" // querying the host; no trustworthy answer yet
  | "branch" // a named branch is checked out (see `branch`)
  | "detached" // detached HEAD — no branch to name
  | "not-git" // explicit evidence the workspace is not a git checkout
  | "unknown"; // no host / offline / ambiguous — not a claim about the repo

/** Live git + PR status for the composer workspace bar. */
export interface ComposerGitStatus {
  /** Named checkout branch when `branchState === "branch"`, else `null`. */
  branch: string | null;
  branchState: ComposerBranchState;
  /** `true` = linked worktree, `false` = the repo's main worktree, `null` = unknown. */
  isWorktree: boolean | null;
  /** Matched worktree root path, or `null` when unresolved. */
  worktreePath: string | null;
  /**
   * Static branch recorded at session creation (`session.gitBranch`). Historical
   * metadata only — never substituted for the live branch, surfaced separately.
   */
  creationBranch: string | null;
  /** `owner/repo` from the git remote, or `null`. */
  repoNameWithOwner: string | null;
  prCount: number;
  prNumber: number | null;
  /** Re-read live worktree + PR state from the host. */
  refresh: () => void;
  refreshing: boolean;
}

/**
 * Resolve the session's live checkout branch and worktree status.
 *
 * The branch comes from the host's `git worktree list` (via
 * {@link useSessionWorktrees}), matched to the session's workspace — the real
 * checked-out branch, distinct from a PR head. {@link useGithubInfo} supplies
 * only PR/repo metadata: its `branch` field can be a PR head ref, so it is not
 * trusted for the live branch. `not-git` is set only on explicit evidence;
 * offline / ambiguous / an empty list stay `unknown` rather than claiming the
 * workspace is not a repo.
 *
 * @param sessionId - Server session id (`null` on the pre-session window).
 * @param hostId - Host the session's workspace lives on.
 * @param workspace - Absolute workspace path.
 * @param creationBranch - Static `session.gitBranch`, surfaced as history only.
 */
export function useComposerGitStatus({
  sessionId,
  hostId,
  workspace,
  creationBranch = null,
}: {
  sessionId: string | null | undefined;
  hostId: string | null | undefined;
  workspace: string | null | undefined;
  creationBranch?: string | null;
}): ComposerGitStatus {
  const worktrees = useSessionWorktrees(sessionId, hostId, workspace);
  const github = useGithubInfo(sessionId ?? undefined);
  const info = github.data;
  const result = worktrees.data;

  let branchState: ComposerBranchState;
  let branch: string | null = null;
  let isWorktree: boolean | null = null;
  let worktreePath: string | null = null;

  if (!hostId || !workspace) {
    branchState = "unknown";
  } else if (worktrees.isPlaceholderData || result === undefined) {
    // Placeholder = a stale prior-key result on a host/workspace switch; treat as
    // still loading rather than advertise the old workspace's branch as live.
    branchState = worktrees.isLoading || worktrees.isPlaceholderData ? "loading" : "unknown";
  } else if (result.status === "not_git") {
    branchState = "not-git";
  } else if (result.status === "unknown" || result.worktrees.length === 0) {
    // Ambiguous git failure, or an OK-but-empty list — neither proves non-git.
    branchState = "unknown";
  } else {
    const matched = matchWorktree(result.worktrees, workspace);
    if (matched === null) {
      branchState = "unknown";
    } else {
      isWorktree = !matched.is_main;
      worktreePath = matched.path;
      if (matched.detached || !matched.branch) {
        branchState = "detached";
      } else {
        branchState = "branch";
        branch = matched.branch;
      }
    }
  }

  const prs = info?.prs;
  const prNumber = prs?.[0]?.number ?? info?.pr?.number ?? null;
  const prCount = prs?.length ?? (prNumber !== null ? 1 : 0);

  const worktreeRefetch = worktrees.refetch;
  const githubRefetch = github.refetch;
  const refresh = useCallback(() => {
    void worktreeRefetch();
    void githubRefetch();
  }, [worktreeRefetch, githubRefetch]);

  return {
    branch,
    branchState,
    isWorktree,
    worktreePath,
    creationBranch: creationBranch?.trim() || null,
    repoNameWithOwner: info?.repo?.name_with_owner ?? null,
    prCount,
    prNumber,
    refresh,
    refreshing: worktrees.isFetching || github.isFetching,
  };
}
