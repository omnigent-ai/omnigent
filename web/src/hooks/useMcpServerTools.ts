import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";

export interface McpServerTools {
  tools: { name: string; description: string | null }[];
  connection: "connected" | "needs_auth" | "unreachable" | "timeout" | "unsupported";
  truncated: boolean;
}

export function useMcpServerTools(
  hostId: string,
  harness: string,
  server: string,
  plugin: string | undefined,
  { enabled }: { enabled: boolean },
) {
  return useQuery({
    queryKey: ["mcp-tools", hostId, harness, server, plugin],
    queryFn: async ({ signal }): Promise<McpServerTools> => {
      const response = await authenticatedFetch(
        `/v1/hosts/${encodeURIComponent(hostId)}/mcp-servers/tools`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ harness, server, plugin }),
          signal,
        },
      );
      if (!response.ok)
        throw new ApiError(`${response.status} ${response.statusText}`, response.status, null);
      return (await response.json()) as McpServerTools;
    },
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
    refetchOnWindowFocus: false,
  });
}
