/**
 * Client for the GitLab OAuth connection endpoints
 * (``/v1/connections/gitlab/*``).
 *
 * The configured GitLab instance may be GitLab.com, GitLab Dedicated, or a
 * self-managed deployment. The server owns the OAuth redirect and token flow.
 */

import { withBasePath } from "./basePath";
import { authenticatedFetch } from "./identity";

/** Shape of ``GET /v1/connections/gitlab/status``. */
export interface GitlabConnectionStatus {
  /** Whether GitLab OAuth is configured on the server. */
  enabled: boolean;
  /** Whether the current user has connected this configured instance. */
  connected: boolean;
  /** Connected GitLab username, or null when not connected. */
  login: string | null;
  /** Canonical URL of the configured GitLab instance. */
  host: string | null;
  /** Space-separated granted OAuth scopes, or null. */
  scopes: string | null;
  /** Unix epoch seconds the account was connected, or null. */
  connected_at: number | null;
}

/** Fetch the current user's configured GitLab connection status. */
export async function fetchGitlabStatus(): Promise<GitlabConnectionStatus> {
  const res = await authenticatedFetch("/v1/connections/gitlab/status");
  if (!res.ok) throw new Error(`GitLab status failed: ${res.status}`);
  return (await res.json()) as GitlabConnectionStatus;
}

/** Start the OAuth redirect to the deployment-configured GitLab instance. */
export function beginGitlabConnect(returnTo: string): void {
  const url = `/v1/connections/gitlab/connect?return_to=${encodeURIComponent(returnTo)}`;
  window.location.href = withBasePath(url);
}

/** Disconnect the current user's GitLab account. */
export async function disconnectGitlab(): Promise<void> {
  const res = await authenticatedFetch("/v1/connections/gitlab/disconnect", { method: "POST" });
  if (!res.ok) throw new Error(`GitLab disconnect failed: ${res.status}`);
}
