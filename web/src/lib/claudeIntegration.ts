/**
 * Client for the Claude subscription endpoints (``/v1/connections/claude/*``).
 *
 * Lets a signed-in user connect their own Claude subscription so their managed
 * sandboxes run Claude Code as them. Claude offers no OAuth redirect for
 * third-party servers, so the user pastes the token ``claude setup-token``
 * prints; status and disconnect mirror ``githubIntegration.ts``.
 */

import { authenticatedFetch } from "./identity";

/** Shape of ``GET /v1/connections/claude/status``. */
export interface ClaudeConnectionStatus {
  /** Whether Claude subscription connect is configured on the server. */
  enabled: boolean;
  /** Whether the current user has connected a subscription. */
  connected: boolean;
  /** Unix epoch seconds the subscription was connected, or null. */
  connected_at: number | null;
}

/** Fetch the current user's Claude connection status. */
export async function fetchClaudeStatus(): Promise<ClaudeConnectionStatus> {
  const res = await authenticatedFetch("/v1/connections/claude/status");
  if (!res.ok) {
    throw new Error(`Claude status failed: ${res.status}`);
  }
  return (await res.json()) as ClaudeConnectionStatus;
}

/**
 * Save the token ``claude setup-token`` printed, replacing any earlier one.
 * Rejects with the server's explanation when the value is not a setup token.
 */
export async function saveClaudeToken(token: string): Promise<ClaudeConnectionStatus> {
  const res = await authenticatedFetch("/v1/connections/claude/token", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    throw new Error(
      typeof body?.detail === "string" ? body.detail : `Claude token save failed: ${res.status}`,
    );
  }
  return (await res.json()) as ClaudeConnectionStatus;
}

/** Disconnect the current user's Claude subscription. */
export async function disconnectClaude(): Promise<void> {
  const res = await authenticatedFetch("/v1/connections/claude/disconnect", {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(`Claude disconnect failed: ${res.status}`);
  }
}
