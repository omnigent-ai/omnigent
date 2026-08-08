import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/**
 * One branch of a repository, as returned by
 * ``GET /v1/hosts/{id}/branches``. Offered as the base a new worktree
 * is cut from.
 */
export interface HostBranch {
  /**
   * Short ref name, e.g. ``"main"`` for a local branch or
   * ``"origin/release-1.2"`` for a remote-tracking one.
   */
  name: string;
  /** ``true`` for the branch checked out in the main work tree. */
  is_current: boolean;
  /**
   * ``true`` for a remote-tracking branch. Still a valid base — the
   * host fetches a base that isn't locally resolvable.
   */
  is_remote: boolean;
}

interface HostBranchesResponse {
  object: string;
  data: HostBranch[];
}

/**
 * Fetch the branches of a repository on a host.
 *
 * A 400 response means the path is not a git repository (or git
 * failed) — the base-branch field falls back to free text, so we
 * resolve to an empty list rather than throwing. Other non-OK
 * responses throw so React Query surfaces the error.
 *
 * @param hostId Host identifier, e.g. ``"host_a1b2..."``.
 * @param repoPath Absolute path inside the repo to list branches for.
 * @returns The repository's branches (most recent first), or ``[]``
 *   when the path is not a git repository.
 */
async function fetchHostBranches(hostId: string, repoPath: string): Promise<HostBranch[]> {
  const params = new URLSearchParams({ path: repoPath });
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/branches?${params.toString()}`,
  );
  if (res.status === 400) {
    // Not a git repository — no branches to offer.
    return [];
  }
  if (!res.ok) {
    throw new Error(`host branches fetch failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as HostBranchesResponse;
  return body.data;
}

/**
 * React Query hook: list the branches of a repository on a host.
 *
 * Lazy — only fires when both ``hostId`` and ``repoPath`` are set.
 * Cached per (host, repoPath). A non-git path resolves to an empty
 * list (see {@link fetchHostBranches}).
 *
 * @param hostId Host id, e.g. ``"host_a1b2..."``. ``null`` disables.
 * @param repoPath Absolute repo path. ``null`` disables.
 * @returns React Query result with ``data: HostBranch[]``.
 */
export function useHostBranches(hostId: string | null, repoPath: string | null) {
  return useQuery({
    queryKey: ["host-branches", hostId, repoPath],
    queryFn: () => fetchHostBranches(hostId as string, repoPath as string),
    enabled: hostId !== null && repoPath !== null && repoPath !== "",
    staleTime: 5_000,
    placeholderData: (prev) => prev,
  });
}
