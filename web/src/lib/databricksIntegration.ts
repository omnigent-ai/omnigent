/**
 * Client for the Databricks integration endpoints
 * (``/v1/connections/databricks/*``).
 *
 * Lets a signed-in user connect their Databricks workspace so their managed
 * sandboxes reach the Databricks AI Gateway (MCP + model serving) as them. The
 * connect flow is a full-page redirect to the workspace's OAuth endpoint (the
 * server owns the U2M + PKCE handshake); status and disconnect are JSON.
 * Mirrors ``githubIntegration.ts``.
 */

import { authenticatedFetch } from "./identity";

/** Shape of ``GET /v1/connections/databricks/status``. */
export interface DatabricksConnectionStatus {
  /** Whether Databricks Connect is configured on the server. */
  enabled: boolean;
  /** Whether the current user has connected a workspace. */
  connected: boolean;
  /** Connected workspace origin (``https://…``), or null. */
  workspace_host: string | null;
  /** Connected Databricks user (email), or null. */
  databricks_user: string | null;
  /** Unix epoch seconds the workspace was connected, or null. */
  connected_at: number | null;
}

/** Fetch the current user's Databricks connection status. */
export async function fetchDatabricksStatus(): Promise<DatabricksConnectionStatus> {
  const res = await authenticatedFetch("/v1/connections/databricks/status");
  if (!res.ok) {
    throw new Error(`Databricks status failed: ${res.status}`);
  }
  return (await res.json()) as DatabricksConnectionStatus;
}

/**
 * Begin the connect flow by navigating to the server's connect endpoint, which
 * redirects the browser to the workspace's OAuth consent. Databricks is
 * multi-workspace, so the user supplies their workspace URL (host or full
 * ``https://…``). ``returnTo`` is where the callback lands afterwards.
 */
export function beginDatabricksConnect(workspace: string, returnTo: string): void {
  const params = new URLSearchParams({ workspace, return_to: returnTo });
  window.location.href = `/v1/connections/databricks/connect?${params.toString()}`;
}

/** Disconnect the current user's Databricks workspace. */
export async function disconnectDatabricks(): Promise<void> {
  const res = await authenticatedFetch("/v1/connections/databricks/disconnect", {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(`Databricks disconnect failed: ${res.status}`);
  }
}
