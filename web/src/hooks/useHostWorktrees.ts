import { useQuery } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";

/**
 * One worktree of a repository, as returned by
 * ``GET /v1/hosts/{id}/worktrees``. Mirrors the host's
 * ``git worktree list`` output.
 */
export interface HostWorktree {
  /**
   * Absolute worktree directory on the host, e.g.
   * ``"/Users/alice/myrepo-worktrees/feature-login"``.
   */
  path: string;
  /**
   * Checked-out branch without the ``refs/heads/`` prefix, e.g.
   * ``"feature/login"``. ``null`` when the worktree is in
   * detached-HEAD state.
   */
  branch: string | null;
  /**
   * ``true`` for the repository's main work tree. The picker hides
   * it — starting "in the main repo" is just picking the directory.
   */
  is_main: boolean;
  /** ``true`` when the worktree has a detached HEAD (no branch). */
  detached: boolean;
}

interface HostWorktreesResponse {
  object: string;
  data: HostWorktree[];
}

/**
 * Fetch the git worktrees of a repository on a host.
 *
 * A 400 response means the path is not a git repository (or git
 * failed) — the picker treats that as "no worktrees here", so we
 * resolve to an empty list rather than throwing. Other non-OK
 * responses throw so React Query surfaces the error.
 *
 * @param hostId Host identifier, e.g. ``"host_a1b2..."``.
 * @param repoPath Absolute path inside the repo to list worktrees for.
 * @returns The repository's worktrees (main first), or ``[]`` when the
 *   path is not a git repository.
 */
async function fetchHostWorktrees(hostId: string, repoPath: string): Promise<HostWorktree[]> {
  const params = new URLSearchParams({ path: repoPath });
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/worktrees?${params.toString()}`,
  );
  if (res.status === 400) {
    // Not a git repository — no worktrees to offer.
    return [];
  }
  if (!res.ok) {
    throw new Error(`host worktrees fetch failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as HostWorktreesResponse;
  return body.data;
}

/**
 * React Query hook: list the git worktrees of a repository on a host.
 *
 * Lazy — only fires when both ``hostId`` and ``repoPath`` are set.
 * Cached per (host, repoPath). A non-git path resolves to an empty
 * list (see {@link fetchHostWorktrees}).
 *
 * @param hostId Host id, e.g. ``"host_a1b2..."``. ``null`` disables.
 * @param repoPath Absolute repo path. ``null`` disables.
 * @returns React Query result with ``data: HostWorktree[]``.
 */
export function useHostWorktrees(hostId: string | null, repoPath: string | null) {
  return useQuery({
    queryKey: ["host-worktrees", hostId, repoPath],
    queryFn: () => fetchHostWorktrees(hostId as string, repoPath as string),
    enabled: hostId !== null && repoPath !== null && repoPath !== "",
    staleTime: 5_000,
    placeholderData: (prev) => prev,
  });
}
