// The MCP connections a session's running harness holds, and reconnecting one.

import {
  skipToken,
  useMutation,
  useMutationState,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** One MCP server as the session's harness reports it. */
export interface LiveMcpServer {
  /** The harness's name for it; plugin servers read `plugin:<plugin>:<server>`. */
  name: string;
  /** `connected`, `failed`, `needs-auth`, `pending`, or `disabled`; others pass through. */
  status: string;
  error?: string | null;
  scope?: string | null;
}

export interface SessionLiveMcpServers {
  /** The harness answered, so `servers` is current. */
  live: boolean;
  reason?: "not_running" | "unsupported" | "unavailable" | null;
  detail?: string | null;
  servers: LiveMcpServer[];
  /** On a reconnect response, the harness's reason it refused. */
  reconnect_error?: string | null;
}

/** A reconnect that did not leave its server connected. */
export interface LiveMcpReconnectError {
  name: string;
  message: string;
}

interface LiveMcpReconnectTarget {
  sessionId: string;
  name: string;
}

// Re-read while a server is still connecting, so the row settles without a reopen.
const LIVE_MCP_PENDING_POLL_MS = 2_000;
const LIVE_MCP_RECONNECT_MUTATION_KEY = ["live-mcp-reconnect"] as const;

// A server that predates the route answers its generic 404; a missing session names itself.
const ROUTE_NOT_FOUND_MESSAGES = new Set([
  "Not found",
  "Not found.",
  "Not Found",
  "404",
  "404 Not Found",
]);

export function sessionLiveMcpServersQueryKey(sessionId: string | null) {
  return ["session-live-mcp-servers", sessionId] as const;
}

function sessionLiveMcpServersPath(sessionId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/live-mcp-servers`;
}

async function fetchSessionLiveMcpServers(
  sessionId: string,
  signal: AbortSignal,
): Promise<SessionLiveMcpServers> {
  const response = await authenticatedFetch(sessionLiveMcpServersPath(sessionId), { signal });
  if (!response.ok) {
    const failure = await apiErrorFromResponse(response);
    // A server without this route keeps the panel's inventory-only view.
    if (response.status === 404 && ROUTE_NOT_FOUND_MESSAGES.has(failure.message)) {
      return { live: false, reason: "unsupported", servers: [] };
    }
    throw failure;
  }
  return (await response.json()) as SessionLiveMcpServers;
}

async function postLiveMcpReconnect(
  sessionId: string,
  name: string,
): Promise<SessionLiveMcpServers> {
  const response = await authenticatedFetch(`${sessionLiveMcpServersPath(sessionId)}/reconnect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as SessionLiveMcpServers;
}

/** Read the session harness's MCP connections; re-read on each enable. */
export function useSessionLiveMcpServers(sessionId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: sessionLiveMcpServersQueryKey(sessionId),
    queryFn:
      enabled && sessionId !== null
        ? ({ signal }) => fetchSessionLiveMcpServers(sessionId, signal)
        : skipToken,
    staleTime: 0,
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.servers.some((server) => server.status === "pending")
        ? LIVE_MCP_PENDING_POLL_MS
        : false,
  });
}

/** Reconnect one of the session harness's MCP servers; the panel shows the outcome. */
export function useReconnectLiveMcpServer(sessionId: string | null) {
  const client = useQueryClient();
  // Keyed by session, so switching away and back keeps each session's failure.
  const [errors, setErrors] = useState<ReadonlyMap<string, LiveMcpReconnectError>>(new Map());
  const setError = useCallback((target: string, error: LiveMcpReconnectError | null) => {
    setErrors((current) => {
      if (error === null && !current.has(target)) return current;
      const next = new Map(current);
      if (error === null) next.delete(target);
      else next.set(target, error);
      return next;
    });
  }, []);
  // Each reconnect carries its session, so one that settles after a switch stays with it.
  const { mutate } = useMutation({
    mutationKey: LIVE_MCP_RECONNECT_MUTATION_KEY,
    mutationFn: ({ sessionId: target, name }: LiveMcpReconnectTarget) =>
      postLiveMcpReconnect(target, name),
    onMutate: ({ sessionId: target }) => setError(target, null),
    onSuccess: async (result, { sessionId: target, name }) => {
      const { reconnect_error: reconnectError, ...snapshot } = result;
      const queryKey = sessionLiveMcpServersQueryKey(target);
      // A poll that started before the reconnect would overwrite its newer snapshot.
      await client.cancelQueries({ queryKey });
      client.setQueryData(queryKey, snapshot);
      if (reconnectError) setError(target, { name, message: reconnectError });
    },
    onError: (failure: Error, { sessionId: target, name }) => {
      setError(target, { name, message: failure.message || "Couldn’t reconnect. Try again." });
      // The harness may have changed state before the request failed.
      void client.invalidateQueries({ queryKey: sessionLiveMcpServersQueryKey(target) });
    },
  });
  // The observer tracks only its latest call, so ask the cache what each session has running.
  const pendingTargets = useMutationState({
    filters: { mutationKey: LIVE_MCP_RECONNECT_MUTATION_KEY, status: "pending" },
    select: (pending) => pending.state.variables as LiveMcpReconnectTarget | undefined,
  });
  const reconnect = useCallback(
    (name: string) => {
      if (sessionId !== null) mutate({ sessionId, name });
    },
    [mutate, sessionId],
  );
  const clearError = useCallback(() => {
    if (sessionId !== null) setError(sessionId, null);
  }, [sessionId, setError]);
  const pending = pendingTargets.find((target) => target?.sessionId === sessionId);
  return {
    reconnect,
    reconnectingName: pending?.name ?? null,
    error: (sessionId !== null && errors.get(sessionId)) || null,
    clearError,
  };
}

/** The harness's name for a host inventory server; the CLI prefixes plugin servers. */
export function liveMcpNameForHostServer(server: { name: string; plugin?: string | null }): string {
  return server.plugin ? `plugin:${server.plugin}:${server.name}` : server.name;
}

/** The `/mcp` row fields that carry one server's live connection. */
export function liveMcpRowFields(server: LiveMcpServer): {
  live: { status: string; error?: string | null };
  liveName: string;
} {
  return { live: { status: server.status, error: server.error }, liveName: server.name };
}

/** Panel note for a live read that has no servers to show, or nothing. */
export function liveMcpStatusNote(
  result: SessionLiveMcpServers | undefined,
  failed: boolean,
): string | undefined {
  if (failed) return "Couldn’t read this session’s live MCP connections.";
  if (result === undefined || result.live) return undefined;
  if (result.reason === "not_running")
    return "Live connection status appears once this session’s agent is running; send a message to start it.";
  if (result.reason === "unavailable")
    return `Couldn’t read this session’s live MCP connections${result.detail ? `: ${result.detail}` : "."}`;
  return undefined;
}
