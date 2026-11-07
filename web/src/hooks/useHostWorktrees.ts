import { queryOptions, useQuery } from "@tanstack/react-query";
import { useRef } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";

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
  /** Unix epoch seconds of the worktree HEAD commit. Missing on older hosts. */
  updated_at?: number | null;
}

/** Windows drive-letter absolute path, e.g. ``C:\repo`` / ``C:/repo``. */
const WINDOWS_ABS_PATH = /^[A-Za-z]:[/\\]/;

/**
 * Canonicalize a path for comparison, mirroring the backend boundary check in
 * `_workspace_validation._is_subpath_of`: when either side is a Windows
 * absolute path, treat ``\`` as a separator and drive-letter case as
 * insignificant; on POSIX keep case and treat ``\`` as a legal filename char.
 * Trailing separators are dropped so they never affect identity.
 */
function canonicalizePath(path: string, windows: boolean): string {
  const canon = windows ? path.replace(/\\/g, "/").toLowerCase() : path;
  return canon.replace(/\/+$/, "");
}

type MatchKind = { kind: "exact" | "contains"; length: number } | { kind: "none" };

/** How ``workspace`` relates to a worktree ``root``, platform-aware. */
function matchKind(root: string, workspace: string): MatchKind {
  const windows = WINDOWS_ABS_PATH.test(root) || WINDOWS_ABS_PATH.test(workspace);
  const r = canonicalizePath(root, windows);
  const w = canonicalizePath(workspace, windows);
  if (w === r) return { kind: "exact", length: r.length };
  // Boundary-aware containment: `/a/foo` is not under `/a/fo` (prefix collision).
  if (w.startsWith(`${r}/`)) return { kind: "contains", length: r.length };
  return { kind: "none" };
}

/**
 * The worktree whose root contains ``workspace`` — exact match, else the
 * deepest root ``workspace`` sits under (a workspace bound to a subdir of a
 * worktree). ``null`` when nothing contains it. Path comparison is
 * platform-aware (see {@link matchKind}).
 */
export function matchWorktree(worktrees: HostWorktree[], workspace: string): HostWorktree | null {
  let best: HostWorktree | null = null;
  let bestLength = -1;
  for (const wt of worktrees) {
    const match = matchKind(wt.path, workspace);
    if (match.kind === "exact") return wt;
    if (match.kind === "contains" && match.length > bestLength) {
      best = wt;
      bestLength = match.length;
    }
  }
  return best;
}

interface VerifiedGitWorktreeCache {
  hostId: string;
  roots: string[];
  worktrees: HostWorktree[];
}

function normalizedHostPath(path: string): string {
  const normalized = path.replace(/\\/g, "/").replace(/\/+$/, "");
  return /^[A-Za-z]:\//.test(normalized) ? normalized.toLowerCase() : normalized;
}

export function pathIsWithinWorktree(path: string, root: string): boolean {
  const candidate = normalizedHostPath(path);
  const boundary = normalizedHostPath(root);
  return candidate === boundary || candidate.startsWith(`${boundary}/`);
}

/**
 * Keep a verified Git repository visible while a nested path is loading.
 * An explicit non-Git result clears the cache.
 */
export function useVerifiedGitWorktrees({
  hostId,
  requestedPath,
  worktrees,
  resolved,
}: {
  hostId: string | null;
  requestedPath: string | null;
  worktrees: HostWorktree[] | undefined;
  resolved: boolean;
}): HostWorktree[] {
  const cacheRef = useRef<VerifiedGitWorktreeCache | null>(null);
  // The listing includes the main checkout even when no linked worktrees exist.
  const directlyVerified = resolved && worktrees !== undefined && worktrees.length > 0;
  if (resolved) {
    cacheRef.current =
      directlyVerified && hostId !== null && worktrees !== undefined
        ? { hostId, roots: worktrees.map((worktree) => worktree.path), worktrees }
        : null;
  }
  const cache = cacheRef.current;
  const cachedMatch =
    cache !== null &&
    cache.hostId === hostId &&
    requestedPath !== null &&
    cache.roots.some((root) => pathIsWithinWorktree(requestedPath, root));

  if (resolved) return directlyVerified ? (worktrees ?? []) : [];
  return cachedMatch ? (cache?.worktrees ?? []) : [];
}

interface HostWorktreesResponse {
  object: string;
  data: HostWorktree[];
}

class HostWorktreesFetchError extends ApiError {
  readonly retryable: boolean;

  constructor(status: number, retryable: boolean) {
    super(`host worktrees fetch failed: HTTP ${status}`, status, null);
    this.retryable = retryable;
  }
}

/**
 * Fetch the git worktrees of a repository on a host.
 *
 * Only an explicit not-a-repo 400 resolves to an empty list. Other
 * failures throw so a transient git failure cannot masquerade as a
 * non-git workspace and discard the user's worktree selection.
 *
 * @param hostId Host identifier, e.g. ``"host_a1b2..."``.
 * @param repoPath Absolute path inside the repo to list worktrees for.
 * @param signal Optional cancellation signal for an abandoned path query.
 * @returns The repository's worktrees (main first), or ``[]`` when the
 *   path is not a git repository.
 */
export async function fetchHostWorktrees(
  hostId: string,
  repoPath: string,
  signal?: AbortSignal,
): Promise<HostWorktree[]> {
  const params = new URLSearchParams({ path: repoPath });
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/worktrees?${params.toString()}`,
    { signal },
  );
  const errorText = res.ok ? "" : await res.text().catch(() => "");
  if (res.status === 400) {
    if (/not a git (?:repo|repository)/i.test(errorText)) return [];
  }
  if (!res.ok) {
    const exhaustedHostBudget =
      res.status === 504 ||
      (res.status === 409 && /did not respond to worktree listing within \d+s/i.test(errorText));
    throw new HostWorktreesFetchError(res.status, res.status !== 400 && !exhaustedHostBudget);
  }
  const body = (await res.json()) as HostWorktreesResponse;
  return body.data;
}

// A 400 is the host's answer for this path. A host timeout may already have
// consumed its 150-second budget, so immediate retries only multiply that wait.
function shouldRetryHostWorktrees(failureCount: number, error: Error): boolean {
  if (error instanceof HostWorktreesFetchError && !error.retryable) return false;
  return failureCount < 3;
}

/** Shared query options for single-path and batched recent-workspace reads. */
export function hostWorktreesQueryOptions(hostId: string, repoPath: string) {
  return queryOptions({
    queryKey: ["host-worktrees", hostId, repoPath] as const,
    queryFn: ({ signal }) => fetchHostWorktrees(hostId, repoPath, signal),
    staleTime: 5_000,
    retry: shouldRetryHostWorktrees,
  });
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
  const enabled = hostId !== null && repoPath !== null && repoPath !== "";
  return useQuery({
    ...hostWorktreesQueryOptions(hostId ?? "", repoPath ?? ""),
    enabled,
  });
}
