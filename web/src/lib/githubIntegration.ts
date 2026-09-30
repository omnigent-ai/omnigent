/**
 * Client for the GitHub App integration endpoints
 * (``/v1/connections/github/*``).
 *
 * Lets a signed-in user connect their GitHub account so their managed
 * sandboxes authenticate ``gh`` / git as them and receive their public
 * SSH keys. The connect flow is a full-page redirect to GitHub (the
 * server owns the OAuth handshake); status and disconnect are JSON.
 */

import { withBasePath } from "./basePath";
import {
  fetchConnectionBranches,
  fetchConnectionRepos,
  type ConnectionBranchList,
  type ConnectionRepo,
  type ConnectionRepoList,
} from "./connectionsApi";
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
  window.location.href = withBasePath(url);
}

/** @deprecated Use ``ConnectionRepo`` from ``connectionsApi``. Removal: 0.19.0. */
export type GithubRepo = ConnectionRepo;

/** @deprecated Use ``ConnectionRepoList`` from ``connectionsApi``. Removal: 0.19.0. */
export type GithubRepoList = ConnectionRepoList;

/** @deprecated Use ``ConnectionBranchList`` from ``connectionsApi``. Removal: 0.19.0. */
export type GithubBranchList = ConnectionBranchList;

/**
 * Fetch the repos the current user can access (App-scoped, newest first).
 *
 * @deprecated Use ``fetchConnectionRepos("github")`` from ``connectionsApi``. Removal: 0.19.0.
 */
export function fetchGithubRepos(): Promise<GithubRepoList> {
  return fetchConnectionRepos("github");
}

/**
 * Fetch the branch names for ``fullName`` (``owner/repo``).
 *
 * @deprecated Use ``fetchConnectionBranches("github", fullName)`` from ``connectionsApi``.
 * Removal: 0.19.0.
 */
export function fetchGithubBranches(fullName: string): Promise<GithubBranchList> {
  return fetchConnectionBranches("github", fullName);
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
