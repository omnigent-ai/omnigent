// Typed client for the `/v1/agents` endpoints introduced for git-based
// agent import. Mirrors `omnigent/server/routes/agents.py`.
//
// All requests go through the existing Vite `/v1` proxy so no proxy
// changes are needed here.

import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

/**
 * An agent object as returned by the AP server. The four `git_*` fields
 * are only present when the agent was imported from a git repository.
 */
export interface AgentObject {
  id: string;
  name: string;
  version?: number;
  git_url?: string | null;
  git_ref?: string | null;
  git_subpath?: string | null;
  git_commit?: string | null;
  git_host_id?: string | null;
}

/**
 * Build an {@link ApiError} from a non-OK response, preferring the
 * server's `error.message` / `error.code` over the bare status line.
 * Falls back to ``"<status> <statusText>"`` when the body is missing or
 * not the AP error shape.
 */
async function errorFromResponse(res: Response): Promise<ApiError> {
  let message = `${res.status} ${res.statusText}`;
  let code: string | null = null;
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    if (body.error?.message) message = body.error.message;
    if (body.error?.code) code = body.error.code;
  } catch {
    // Non-JSON / empty body — keep the status-line fallback.
  }
  return new ApiError(message, res.status, code);
}

/**
 * Import an agent from a git repository via POST /v1/agents/import-git.
 *
 * @param input.gitUrl - The git repository URL (required).
 * @param input.gitRef - The git ref to check out (branch, tag, commit); null if omitted.
 * @param input.gitSubpath - Subdirectory within the repo containing the agent spec; null if omitted.
 * @returns The imported AgentObject.
 */
export async function importAgentFromGit(input: {
  gitUrl: string;
  gitRef?: string;
  gitSubpath?: string;
  hostId: string;
}): Promise<AgentObject> {
  const res = await authenticatedFetch("/v1/agents/import-git", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      git_url: input.gitUrl,
      git_ref: input.gitRef ?? null,
      git_subpath: input.gitSubpath ?? null,
      host_id: input.hostId,
    }),
  });
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as AgentObject;
}

/**
 * Refresh an existing git-backed agent from its upstream repository via
 * POST /v1/agents/{agentId}/refresh.
 *
 * @param agentId - The durable agent id, e.g. "ag_abc123".
 * @returns The updated AgentObject.
 */
export async function refreshAgent(agentId: string): Promise<AgentObject> {
  const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(agentId)}/refresh`, {
    method: "POST",
  });
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as AgentObject;
}
