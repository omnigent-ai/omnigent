// TanStack Query wrapper for a session's read-only capabilities.
//
// Backed by `GET /v1/sessions/{id}/capabilities`, a consolidated,
// read-only observability endpoint returning the merged skills, MCP
// server config, local/builtin function tools, and declared sub-agent
// tree of the agent bound to the session. The endpoint is context-aware:
// an omnigent-spawned named sub-agent child reports THAT sub-agent's
// capabilities, so the query is keyed on `conversationId` — navigating
// into a sub-agent re-fetches and shows the child's capabilities.

import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** Name + one-line description of a skill available to the session. */
export interface CapabilitySkill {
  name: string;
  description: string;
  /** Origin of the skill: bundle | workspace | user | plugin | codex | cursor | unknown. */
  source: string;
  /** Usable given the agent's skills_filter (bundled always true; host/plugin iff it passes). */
  in_scope: boolean;
  /** A name-based policy (block_skills/CEL) would DENY loading this skill. */
  blocked: boolean;
}

/** Name + optional one-line description of a single function tool. */
export interface CapabilityTool {
  name: string;
  description?: string | null;
  /** A name-based policy would DENY calling this tool. */
  blocked: boolean;
}

/**
 * An MCP server's secret-free config, connection status, and discovered
 * per-server tool list.
 */
export interface CapabilityMcpServer {
  name: string;
  /** Transport type: "http" (SSE endpoint) or "stdio" (spawned subprocess). */
  transport: string;
  description?: string | null;
  /** HTTP SSE endpoint URL. Only present when transport === "http". */
  url?: string | null;
  /** Executable to spawn. Only present when transport === "stdio". */
  command?: string | null;
  /** Arguments passed to command. Only present when transport === "stdio". */
  args?: string[];
  /** Connection status: "connected" | "failed" | "unknown". */
  status: string;
  /** Failure detail when the server could not be reached. */
  error?: string | null;
  /** Discovered MCP tools exposed by this server. */
  tools: CapabilityTool[];
}

/** A declared sub-agent node in the capabilities tree (recursive). */
export interface SubAgentCapability {
  name?: string | null;
  description?: string | null;
  sub_agents: SubAgentCapability[];
}

/** Consolidated, read-only capabilities of a session's bound agent. */
export interface SessionCapabilities {
  session_id: string;
  agent_id: string;
  /** The dispatched sub-agent name when the session is a named child, else null. */
  sub_agent_name?: string | null;
  skills: CapabilitySkill[];
  mcp_servers: CapabilityMcpServer[];
  local_tools: CapabilityTool[];
  sub_agents: SubAgentCapability[];
}

/** Wire shape of the `GET /v1/sessions/{id}/capabilities` response. */
interface SessionCapabilitiesWire {
  object: "session.capabilities";
  session_id: string;
  agent_id: string;
  sub_agent_name?: string | null;
  skills?: CapabilitySkill[];
  mcp_servers?: CapabilityMcpServer[];
  local_tools?: CapabilityTool[];
  sub_agents?: SubAgentCapability[];
}

/** TanStack Query key for a session's capabilities. */
export function sessionCapabilitiesQueryKey(conversationId: string): readonly unknown[] {
  return ["session-capabilities", conversationId];
}

async function fetchSessionCapabilities(sessionId: string): Promise<SessionCapabilities> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/capabilities`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as SessionCapabilitiesWire;
  return {
    session_id: json.session_id,
    agent_id: json.agent_id,
    sub_agent_name: json.sub_agent_name ?? null,
    skills: json.skills ?? [],
    mcp_servers: json.mcp_servers ?? [],
    local_tools: json.local_tools ?? [],
    sub_agents: json.sub_agents ?? [],
  };
}

/**
 * Fetch the read-only capabilities of the agent bound to a session.
 *
 * Keyed on `conversationId` so opening a sub-agent re-fetches and shows
 * that agent's capabilities. Only fires when `conversationId` is non-null.
 *
 * :param conversationId: The session whose capabilities to retrieve,
 *     or ``null`` to disable the query.
 */
export function useSessionCapabilities(conversationId: string | null) {
  return useQuery({
    queryKey:
      conversationId === null
        ? sessionCapabilitiesQueryKey("__none__")
        : sessionCapabilitiesQueryKey(conversationId),
    queryFn: () => fetchSessionCapabilities(conversationId as string),
    enabled: conversationId !== null,
    staleTime: 30_000,
  });
}
