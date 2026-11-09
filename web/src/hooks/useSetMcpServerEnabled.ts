import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import type { HostMcpServer } from "@/hooks/useHarnessInventory";
import { setMcpServerEnabled, type McpServerEnabledRequest } from "@/lib/mcpServerEnabledApi";

export function useSetMcpServerEnabled(hostId: string) {
  const client = useQueryClient();
  const queryKey = ["host-mcp-servers", hostId];
  return useMutation({
    mutationFn: (body: McpServerEnabledRequest) => setMcpServerEnabled(hostId, body),
    onMutate: async (body) => {
      await client.cancelQueries({ queryKey });
      const matches = (row: HostMcpServer) =>
        row.harness === body.harness &&
        row.name === body.server &&
        (row.plugin ?? undefined) === body.plugin &&
        (row.source_id ?? undefined) === body.source_id;
      const previous = client.getQueryData<HostMcpServer[]>(queryKey)?.find(matches);
      client.setQueryData<HostMcpServer[]>(queryKey, (rows) =>
        rows?.map((row) => (matches(row) ? { ...row, enabled: body.enabled } : row)),
      );
      return { previous, matches };
    },
    onError: (_error, _body, context) => {
      if (context?.previous)
        client.setQueryData<HostMcpServer[]>(queryKey, (rows) =>
          rows?.map((row) => (context.matches(row) ? context.previous! : row)),
        );
      toast.error("Couldn't update MCP server. Please try again.");
    },
    onSuccess: () => toast.success("Applies to new sessions"),
    onSettled: () => client.invalidateQueries({ queryKey }),
  });
}
