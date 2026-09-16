/**
 * Client for the GitHub App integration endpoints
 * (``/v1/connections/github/*``).
 *
 * Lets a signed-in user connect their GitHub account so their managed
 * sandboxes authenticate ``gh`` / git as them and receive their public
 * SSH keys. The connect flow is a full-page redirect to GitHub (the
 * server owns the OAuth handshake); status and disconnect are JSON.
 */

import { authenticatedFetch } from "./identity";

/** Shape of ``GET /v1/connections/github/status``. */
export interface GithubConnectionStatus {
  /** Whether the GitHub App is configured on the server. */
  enabled: boolean;
  /** Whether the current user has connected their account. */
  connected: boolean;
  /** Connected GitHub login, or null when not connected. */
  login: string | null;
  /** Space-separated granted scopes, or null. */
  scopes: string | null;
  /** Unix epoch seconds the account was connected, or null. */
  connected_at: number | null;
  /** The App's install URL, or null when no slug is configured. */
  install_url: string | null;
}

/** Fetch the current user's GitHub connection status. */
export async function fetchGithubStatus(): Promise<GithubConnectionStatus> {
  const res = await authenticatedFetch("/v1/connections/github/status");
  if (!res.ok) {
    throw new Error(`GitHub status failed: ${res.status}`);
  }
  return (await res.json()) as GithubConnectionStatus;
}

/**
 * Begin the connect flow by navigating to the server's connect endpoint,
 * which redirects the browser to GitHub. ``return_to`` is where the
 * callback lands afterwards (defaults to the current settings path).
 */
export function beginGithubConnect(returnTo: string): void {
  const url = `/v1/connections/github/connect?return_to=${encodeURIComponent(returnTo)}`;
  window.location.href = url;
}

/** A repo the connected user can access, from ``GET .../github/repos``. */
export interface GithubRepo {
  /** ``owner/name``, e.g. ``"caffeinelabs/app"``. */
  full_name: string;
  /** HTTPS clone URL, or null. */
  clone_url: string | null;
  /** Default branch, or null. */
  default_branch: string | null;
  /** Whether the repo is private. */
  private: boolean;
  /** ISO-8601 last-push time, or null (list is newest-first). */
  pushed_at: string | null;
}

/** Shape of ``GET /v1/connections/github/repos``. */
export interface GithubRepoList {
  /** False when the user hasn't connected GitHub (repos is then empty). */
  connected: boolean;
  repos: GithubRepo[];
  /** True when the page cap was hit and more repos exist than are returned. */
  truncated?: boolean;
}

/**
 * Fetch the repos the current user can access (App-scoped, newest first).
 * Returns ``connected: false`` with an empty list when GitHub isn't linked,
 * so callers can fall back to a free-text repo URL.
 */
export async function fetchGithubRepos(): Promise<GithubRepoList> {
  const res = await authenticatedFetch("/v1/connections/github/repos");
  if (!res.ok) {
    throw new Error(`GitHub repos failed: ${res.status}`);
  }
  return (await res.json()) as GithubRepoList;
}

/** Shape of ``GET .../github/repos/{owner}/{repo}/branches``. */
export interface GithubBranchList {
  /** False when the user hasn't connected GitHub (branches is then empty). */
  connected: boolean;
  branches: string[];
}

/**
 * Fetch the branch names for ``fullName`` (``owner/repo``), for the
 * per-repo branch picker. Returns ``connected: false`` with an empty list
 * when GitHub isn't linked.
 */
export async function fetchGithubBranches(fullName: string): Promise<GithubBranchList> {
  const res = await authenticatedFetch(`/v1/connections/github/repos/${fullName}/branches`);
  if (!res.ok) {
    throw new Error(`GitHub branches failed: ${res.status}`);
  }
  return (await res.json()) as GithubBranchList;
}

/** Disconnect the current user's GitHub account. */
export async function disconnectGithub(): Promise<void> {
  const res = await authenticatedFetch("/v1/connections/github/disconnect", {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(`GitHub disconnect failed: ${res.status}`);
  }
}
