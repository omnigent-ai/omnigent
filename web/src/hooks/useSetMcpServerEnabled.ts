import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError } from "@/lib/sessionsApi";
import { hostMcpServersQueryKey, type HostMcpServer } from "@/hooks/useHarnessInventory";
import { setMcpServerEnabled, type McpServerEnabledRequest } from "@/lib/mcpServerEnabledApi";

export function useSetMcpServerEnabled(hostId: string) {
  const client = useQueryClient();
  const queryKey = hostMcpServersQueryKey(hostId);
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
    onError: (error, _body, context) => {
      if (context?.previous)
        client.setQueryData<HostMcpServer[]>(queryKey, (rows) =>
          rows?.map((row) => (context.matches(row) ? context.previous! : row)),
        );
      toast.error(mcpEnablementErrorMessage(error));
    },
    onSuccess: () => toast.success("Applies to new sessions"),
    onSettled: () => client.invalidateQueries({ queryKey }),
  });
}

function mcpEnablementErrorMessage(error: Error): string {
  if (error instanceof ApiError) {
    if (error.status === 422) return "This server changed. Refresh and try again.";
    if (error.status === 409) return "Host is offline.";
    if (error.status === 501) return "Update the host to turn MCP servers on or off.";
  }
  return error.message || "Couldn't update MCP server. Please try again.";
}
