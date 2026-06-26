// Client for the standalone (owner-scoped) agents CRUD API, `/v1/agents*`.
//
// These are first-class, session-independent agents managed from the
// sidebar "Agents" section — distinct from the read-only built-ins
// (`GET /v1/agents`) and from session-scoped agents. Create/update reuse
// `buildAgentBundle` (the same `.tar.gz` the new-chat custom-agent flow
// builds) and POST/PUT it as multipart; the server stores it owner-scoped
// so the agent persists across sessions and survives session deletion.

import { type AgentBundleInput, buildAgentBundle } from "@/lib/agentBundle";
import { authenticatedFetch } from "@/lib/identity";

/**
 * TanStack Query key for the caller's standalone agents (`/v1/agents/mine`).
 * Shared by the sidebar manage view and the new-chat picker so a create/delete
 * in either surface invalidates both. The picker query (`["available-agents"]`)
 * is invalidated alongside it.
 */
export const MY_AGENTS_QUERY_KEY = ["my-agents"] as const;

/** Summary of an MCP server attached to an agent (mirrors the API shape). */
export interface AgentMcpServer {
  name: string;
  transport: string;
  description?: string | null;
  url?: string | null;
  command?: string | null;
  args?: string[];
}

/** A standalone, user-owned agent as returned by the agents API. */
export interface ManagedAgent {
  id: string;
  name: string;
  description: string | null;
  harness: string | null;
  version: number;
  created_at: number;
  updated_at: number | null;
  mcp_servers: AgentMcpServer[];
}

async function describeError(res: Response, action: string): Promise<string> {
  let detail = "";
  try {
    const body = (await res.json()) as { error?: string; detail?: string };
    detail = body.error || body.detail || "";
  } catch {
    // non-JSON body; fall back to the status line below
  }
  return detail || `Could not ${action} (HTTP ${res.status}).`;
}

/** List the current user's standalone agents (newest-first). */
export async function listMyAgents(): Promise<ManagedAgent[]> {
  const res = await authenticatedFetch("/v1/agents/mine");
  if (!res.ok) throw new Error(await describeError(res, "load your agents"));
  const body = (await res.json()) as { data: ManagedAgent[] };
  return body.data;
}

/** Create a standalone agent from the given spec (built into a bundle). */
export async function createAgent(input: AgentBundleInput): Promise<ManagedAgent> {
  const bundle = await buildAgentBundle(input);
  const form = new FormData();
  form.append("bundle", bundle);
  if (input.description) form.append("description", input.description);
  const res = await authenticatedFetch("/v1/agents", { method: "POST", body: form });
  if (!res.ok) throw new Error(await describeError(res, "create the agent"));
  return (await res.json()) as ManagedAgent;
}

/** Replace a standalone agent's bundle with a new spec (owner only). */
export async function updateAgent(
  agentId: string,
  input: AgentBundleInput,
): Promise<ManagedAgent> {
  const bundle = await buildAgentBundle(input);
  const form = new FormData();
  form.append("bundle", bundle);
  const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(agentId)}`, {
    method: "PUT",
    body: form,
  });
  if (!res.ok) throw new Error(await describeError(res, "update the agent"));
  return (await res.json()) as ManagedAgent;
}

/** Delete a standalone agent (owner only). */
export async function deleteAgent(agentId: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(agentId)}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 204) {
    throw new Error(await describeError(res, "delete the agent"));
  }
}
