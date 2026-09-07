// TanStack Query wrappers for agent information.
//
// Agents are now derived from session data rather than a standalone
// `/api/agents` endpoint. `fetchAgents()` calls `GET /v1/sessions`
// and extracts unique `{id, name}` pairs from the `agent_id` /
// `agent_name` fields on each session. `useSessionAgent(sessionId)`
// fetches the full `AgentObject` for a single session via
// `GET /v1/sessions/{sessionId}/agent`.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export interface McpServerSummary {
  name: string;
  /** Transport type: "http" (SSE endpoint) or "stdio" (spawned subprocess). */
  transport: string;
  description?: string | null;
  /** HTTP SSE endpoint URL. Only present when transport === "http". */
  url?: string | null;
  /**
   * HTTP headers for the server. Values are always "[REDACTED]" in API
   * responses; only key names are exposed.
   */
  headers?: Record<string, string>;
  /** Executable to spawn. Only present when transport === "stdio". */
  command?: string | null;
  /** Arguments passed to command. Only present when transport === "stdio". */
  args?: string[];
}

export interface PolicySummary {
  /** Policy name as declared in the agent spec, e.g. "block_long_sleep". */
  name: string;
  /** Policy type — "function", "prompt", or "label". */
  type: string;
  /** Phase selectors the policy fires on, e.g. ["tool_call"] or ["request:code_sandbox"]. */
  on: string[];
  /** Short description: callable path for function, first prompt line for prompt, action for label. */
  description?: string | null;
}

export interface Agent {
  id: string;
  /** Human-readable name from the YAML's `name:` field, e.g. "hello_world".
   * Required by the spec_version: 1 parser, so always present. */
  name: string;
  description?: string | null;
  /** Harness/kind, e.g. "claude-native", "codex", "claude_sdk". null when
   * the spec couldn't be loaded. Only populated by `useSessionAgent`
   * (the sessions-derived `useAgents` list leaves it undefined). */
  harness?: string | null;
  /** MCP server declarations from the agent spec. Empty when none configured. */
  mcp_servers?: McpServerSummary[];
  /** Whether the active session's agent bundle can be edited through the UI. */
  mcp_servers_editable?: boolean;
  /** Guardrails policies declared on the agent. Empty when none configured. */
  policies?: PolicySummary[];
  /** Terminal names declared in the spec's `terminals:` block, in
   * declaration order (e.g. ["shell"], or ["zsh", "bash"] for a native
   * session offering the host's installed shells, default first). Gates
   * the "new terminal" affordance: empty means the agent has no terminal
   * access and the UI must not offer creation. Only populated by
   * `useSessionAgent`. */
  terminals?: string[];
}

/** Wire shape of a session list item from `GET /v1/sessions`. */
interface SessionListItemWire {
  id: string;
  agent_id: string;
  agent_name?: string | null;
}

interface SessionsListResponse {
  data: SessionListItemWire[];
  has_more: boolean;
}

/**
 * Fetch unique agents by scanning the sessions list.
 *
 * Calls `GET /v1/sessions?limit=100` and deduplicates by `agent_id`.
 * Sessions without an `agent_name` are skipped (orphaned / deleted
 * agent). The returned `Agent` objects carry only `id` and `name` —
 * `description` and `mcp_servers` require a per-session agent fetch
 * via `useSessionAgent`.
 */
async function fetchAgents(): Promise<Agent[]> {
  const res = await authenticatedFetch("/v1/sessions?limit=100");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as SessionsListResponse;

  const seen = new Map<string, Agent>();
  for (const session of json.data) {
    if (!session.agent_id || seen.has(session.agent_id)) continue;
    seen.set(session.agent_id, {
      id: session.agent_id,
      name: session.agent_name ?? session.agent_id,
    });
  }
  return Array.from(seen.values());
}

/**
 * Fetch the agents list, derived from active sessions.
 *
 * Intended for the landing page (no active session); the session
 * detail page uses `useSessionAgent` for the bound agent instead.
 */
export function useAgents({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: ["agents"],
    queryFn: fetchAgents,
    staleTime: Infinity,
    enabled,
  });
}

/**
 * Wire shape of `AgentObject` returned by
 * `GET /v1/sessions/{sessionId}/agent`.
 */
interface AgentObjectWire {
  id: string;
  object: "agent";
  name: string;
  description?: string | null;
  harness?: string | null;
  mcp_servers?: McpServerSummary[];
  mcp_servers_editable?: boolean;
  policies?: PolicySummary[];
  terminals?: string[];
}

/**
 * Fetch the full agent object for a specific session via
 * `GET /v1/sessions/{sessionId}/agent`.
 *
 * :param sessionId: The session whose bound agent to retrieve.
 */
async function fetchSessionAgent(sessionId: string): Promise<Agent> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/agent`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as AgentObjectWire;
  return {
    id: json.id,
    name: json.name,
    description: json.description,
    harness: json.harness ?? null,
    mcp_servers: json.mcp_servers,
    mcp_servers_editable: json.mcp_servers_editable,
    policies: json.policies,
    terminals: json.terminals,
  };
}

/**
 * Fetch a single agent by session id. Used for session-scoped agents
 * created by `omnigent run --server` which may not appear in the
 * sessions-derived agent list. Only fires when `sessionId` is non-null.
 */
export function useSessionAgent(sessionId: string | null) {
  return useQuery({
    queryKey: ["session-agent", sessionId],
    queryFn: () => fetchSessionAgent(sessionId!),
    enabled: sessionId !== null,
    staleTime: Infinity,
  });
}

export interface UpsertMcpServerInput {
  name: string;
  transport: "http" | "stdio";
  description?: string | null;
  url?: string | null;
  headers?: Record<string, string> | null;
  command?: string | null;
  args?: string[];
}

async function parseMutationError(res: Response): Promise<Error> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; detail?: unknown };
    if (body.error?.message) return new Error(body.error.message);
    if (typeof body.detail === "string") return new Error(body.detail);
  } catch {
    // Fall through to the status text.
  }
  return new Error(`${res.status} ${res.statusText}`);
}

function sessionAgentQueryKey(sessionId: string) {
  return ["session-agent", sessionId];
}

function invalidateSessionAgent(queryClient: ReturnType<typeof useQueryClient>, sessionId: string) {
  void queryClient.invalidateQueries({ queryKey: sessionAgentQueryKey(sessionId) });
}

export function useCreateMcpServer(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: UpsertMcpServerInput) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/agent/mcp-servers`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!res.ok) throw await parseMutationError(res);
      return (await res.json()) as McpServerSummary;
    },
    onSuccess: () => invalidateSessionAgent(queryClient, sessionId),
  });
}

export function useUpdateMcpServer(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      serverName,
      payload,
    }: {
      serverName: string;
      payload: UpsertMcpServerInput;
    }) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/agent/mcp-servers/${encodeURIComponent(
          serverName,
        )}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!res.ok) throw await parseMutationError(res);
      return (await res.json()) as McpServerSummary;
    },
    onSuccess: () => invalidateSessionAgent(queryClient, sessionId),
  });
}

export function useDeleteMcpServer(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (serverName: string) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/agent/mcp-servers/${encodeURIComponent(
          serverName,
        )}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw await parseMutationError(res);
    },
    onSuccess: () => invalidateSessionAgent(queryClient, sessionId),
  });
}
