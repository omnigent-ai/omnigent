/**
 * Client for standalone, owner-scoped MCP servers (`/v1/mcp-servers`).
 *
 * Standalone MCP servers are reusable connections the user registers once
 * (and can verify — connect + list tools) and then selects when creating
 * an agent, instead of re-typing url/headers into every create-agent form.
 *
 * Secret-bearing fields (`headers`, `env`) are sent on write but never
 * returned by the server — list/get responses expose only the keys
 * (`header_keys` / `env_keys`).
 */

import { authenticatedFetch } from "@/lib/identity";

/** TanStack Query key for the caller's standalone MCP servers. */
export const MY_MCP_SERVERS_QUERY_KEY = ["my-mcp-servers"] as const;

/** Safe wire shape of a stored MCP server (no secret values). */
export interface McpServerObject {
  id: string;
  name: string;
  transport: "http" | "stdio";
  description: string | null;
  url: string | null;
  command: string | null;
  args: string[];
  header_keys: string[];
  env_keys: string[];
  created_at: number;
  updated_at: number | null;
}

/** Body for creating or updating a standalone MCP server. */
export interface McpServerInput {
  name: string;
  transport: "http" | "stdio";
  description?: string;
  url?: string;
  headers?: Record<string, string>;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
}

/** One tool discovered on an MCP server. */
export interface McpToolInfo {
  name: string;
  description: string | null;
}

/** Result of a connection verify. */
export interface McpVerifyResult {
  ok: boolean;
  tools: McpToolInfo[];
  error: string | null;
}

async function readError(res: Response, fallback: string): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; detail?: unknown };
    if (body?.error?.message) return body.error.message;
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    // Non-JSON body — fall through to the generic message.
  }
  return fallback;
}

/** List the caller's standalone MCP servers. */
export async function listMyMcpServers(): Promise<McpServerObject[]> {
  const res = await authenticatedFetch("/v1/mcp-servers/mine");
  if (!res.ok) throw new Error(await readError(res, `${res.status} ${res.statusText}`));
  const body = (await res.json()) as { data: McpServerObject[] };
  return body.data;
}

/** Register a new standalone MCP server. */
export async function createMcpServer(input: McpServerInput): Promise<McpServerObject> {
  const res = await authenticatedFetch("/v1/mcp-servers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(await readError(res, "Failed to create MCP server"));
  return (await res.json()) as McpServerObject;
}

/** Replace a standalone MCP server. */
export async function updateMcpServer(
  id: string,
  input: McpServerInput,
): Promise<McpServerObject> {
  const res = await authenticatedFetch(`/v1/mcp-servers/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(await readError(res, "Failed to update MCP server"));
  return (await res.json()) as McpServerObject;
}

/** Delete a standalone MCP server. */
export async function deleteMcpServer(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/mcp-servers/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 404) {
    throw new Error(await readError(res, "Failed to delete MCP server"));
  }
}

/** Full config of a stored server, including secret values (owner only). */
export interface McpServerFullConfig {
  id: string;
  name: string;
  transport: "http" | "stdio";
  description: string | null;
  url: string | null;
  headers: Record<string, string>;
  command: string | null;
  args: string[];
  env: Record<string, string>;
}

/**
 * Fetch a stored server's full config *with* secrets (owner only).
 *
 * Used by the create-agent flow to bake a selected preconfigured server
 * into a new agent bundle — the bundle is built client-side, so the secret
 * values must be retrievable here (same trust boundary as typing them into
 * the create-agent form).
 */
export async function getMcpServerConfig(id: string): Promise<McpServerFullConfig> {
  const res = await authenticatedFetch(`/v1/mcp-servers/${encodeURIComponent(id)}/config`);
  if (!res.ok) throw new Error(await readError(res, "Failed to load MCP server config"));
  return (await res.json()) as McpServerFullConfig;
}

/** Verify an ad-hoc MCP config (before saving): connect + list tools. */
export async function verifyMcpServer(input: McpServerInput): Promise<McpVerifyResult> {
  const res = await authenticatedFetch("/v1/mcp-servers/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(await readError(res, "Failed to verify MCP server"));
  return (await res.json()) as McpVerifyResult;
}

/** Verify a stored MCP server by id using its saved (secret) config. */
export async function verifySavedMcpServer(id: string): Promise<McpVerifyResult> {
  const res = await authenticatedFetch(
    `/v1/mcp-servers/${encodeURIComponent(id)}/verify`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await readError(res, "Failed to verify MCP server"));
  return (await res.json()) as McpVerifyResult;
}
