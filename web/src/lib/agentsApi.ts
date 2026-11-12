import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** An agent installed with {@link installAgentBundle}. */
export interface InstalledAgent {
  id: string;
  name: string;
}

/**
 * Install (or replace) the caller's reusable agent from a `.tar.gz` bundle,
 * the same shape `omnigent agent add` uploads. The server validates the
 * bundle and never runs it; the agent then stays in the new-session picker.
 *
 * @param bundle - Gzipped tarball chosen by the user.
 * @returns The installed agent's id and name.
 * @throws Error carrying the server's message when the install is rejected.
 */
export async function installAgentBundle(bundle: File): Promise<InstalledAgent> {
  const form = new FormData();
  form.append("bundle", bundle, bundle.name);
  const res = await authenticatedFetch("/v1/agents", { method: "POST", body: form });
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json().catch(() => null)) as Partial<InstalledAgent> | null;
  if (typeof body?.id !== "string" || typeof body.name !== "string") {
    throw new Error("Unexpected response from the server when installing the agent.");
  }
  return { id: body.id, name: body.name };
}

export interface AgentMCPConfig {
  transport: string;
  description: string | null;
  url: string | null;
  command: string | null;
  args: string[];
  configured_header_names: string[];
  configured_env_names: string[];
  editable: boolean;
}

export interface AgentConfig {
  id: string;
  name: string;
  version: number;
  description: string | null;
  instructions: string | null;
  model: string | null;
  harness: string | null;
  mcp_servers: Record<string, AgentMCPConfig>;
}

export interface MCPConfigPatch {
  transport?: "http" | "stdio";
  description?: string | null;
  url?: string;
  command?: string;
  args?: string[];
  headers?: Record<string, string | null>;
  env?: Record<string, string | null>;
}

export interface AgentConfigPatch {
  description?: string | null;
  instructions?: string;
  model?: string;
  harness?: string;
  mcp_servers?: Record<string, MCPConfigPatch | null>;
}

export interface AgentConfigRevision {
  config: AgentConfig;
  etag: string | null;
}

export async function getAgentConfig(
  id: string,
  signal?: AbortSignal,
): Promise<AgentConfigRevision> {
  const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(id)}/config`, { signal });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return { config: await res.json(), etag: res.headers.get("ETag") };
}

export async function patchAgentConfig(
  id: string,
  etag: string | null,
  patch: AgentConfigPatch,
): Promise<AgentConfigRevision> {
  const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...(etag ? { "If-Match": etag } : {}) },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return { config: await res.json(), etag: res.headers.get("ETag") };
}
