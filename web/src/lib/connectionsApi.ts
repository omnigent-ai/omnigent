/**
 * Client for the provider-neutral repository endpoints of a git provider
 * connection (``/v1/connections/{provider}/repos``).
 *
 * The new-chat repository picker lists a connected account's repos and their
 * branches through these. Connect, status, and disconnect stay in each
 * provider's own module (``githubIntegration.ts``).
 */

import { authenticatedFetch } from "./identity";

/** A repo the connected user can access, from ``GET /v1/connections/{provider}/repos``. */
export interface ConnectionRepo {
  /** Provider-scoped name, e.g. ``"caffeinelabs/app"`` for GitHub. */
  full_name: string;
  /** HTTPS clone URL, or null when the provider's ``cloneUrlFor`` derives it. */
  clone_url: string | null;
  /** Default branch, or null. */
  default_branch: string | null;
  /** Whether the repo is private. */
  private: boolean;
  /** ISO-8601 last-push time, or null (list is newest-first). */
  pushed_at?: string | null;
}

/** Shape of ``GET /v1/connections/{provider}/repos``. */
export interface ConnectionRepoList {
  /** False when the user hasn't connected the provider (repos is then empty). */
  connected: boolean;
  repos: ConnectionRepo[];
  /** True when the page cap was hit and more repos exist than are returned. */
  truncated?: boolean;
}

/** Shape of ``GET /v1/connections/{provider}/repos/{full_name}/branches``. */
export interface ConnectionBranchList {
  /** False when the user hasn't connected the provider (branches is then empty). */
  connected: boolean;
  branches: string[];
}

/** Encode each ``/``-separated segment of a repo name and keep the slashes. */
function encodeRepoPath(fullName: string): string {
  return fullName.split("/").map(encodeURIComponent).join("/");
}

/**
 * Fetch the repos the current user can access through ``providerId``'s
 * connection, newest first. Returns ``connected: false`` with an empty list when
 * the account isn't linked, so callers can fall back to a free-text repo URL.
 */
export async function fetchConnectionRepos(providerId: string): Promise<ConnectionRepoList> {
  const res = await authenticatedFetch(`/v1/connections/${encodeURIComponent(providerId)}/repos`);
  if (!res.ok) {
    throw new Error(`Connection repos failed (${providerId}): ${res.status}`);
  }
  return (await res.json()) as ConnectionRepoList;
}

/**
 * Fetch the branch names for ``fullName`` (e.g. ``owner/repo``) through
 * ``providerId``'s connection, for the per-repo branch picker. Returns
 * ``connected: false`` with an empty list when the account isn't linked.
 */
export async function fetchConnectionBranches(
  providerId: string,
  fullName: string,
): Promise<ConnectionBranchList> {
  const res = await authenticatedFetch(
    `/v1/connections/${encodeURIComponent(providerId)}/repos/${encodeRepoPath(fullName)}/branches`,
  );
  if (!res.ok) {
    throw new Error(`Connection branches failed (${providerId}): ${res.status}`);
  }
  return (await res.json()) as ConnectionBranchList;
}
