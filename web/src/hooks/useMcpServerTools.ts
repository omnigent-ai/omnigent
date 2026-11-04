import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";

export interface McpServerTools {
  tools: { name: string; description: string | null }[];
  connection: "connected" | "needs_auth" | "unreachable" | "timeout" | "unsupported";
  truncated: boolean;
  /** With `needs_auth`: the harness can sign in to the server, or already has. */
  harness_sign_in?: "available" | "signed_in" | null;
}

/** Matches the host's sign-in status cache, so a sign-in made outside Omnigent shows. */
const SIGN_IN_STALE_MS = 30_000;

export function mcpServerToolsKey(
  hostId: string,
  harness: string,
  server: string,
  plugin: string | undefined,
  sourceId: string | undefined,
) {
  return ["mcp-tools", hostId, harness, server, plugin, sourceId] as const;
}

export function useMcpServerTools(
  hostId: string,
  harness: string,
  server: string,
  plugin: string | undefined,
  { enabled, sourceId }: { enabled: boolean; sourceId?: string },
) {
  return useQuery({
    queryKey: mcpServerToolsKey(hostId, harness, server, plugin, sourceId),
    queryFn: async ({ signal }): Promise<McpServerTools> => {
      const response = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/mcp-servers/tools`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ harness, server, plugin, source_id: sourceId }),
          signal,
        },
      );
      if (!response.ok)
        throw new ApiError(`${response.status} ${response.statusText}`, response.status, null);
      return (await response.json()) as McpServerTools;
    },
    enabled,
    staleTime: (query) =>
      query.state.data?.connection === "needs_auth" ? SIGN_IN_STALE_MS : 5 * 60_000,
    retry: false,
    refetchOnWindowFocus: false,
  });
}
