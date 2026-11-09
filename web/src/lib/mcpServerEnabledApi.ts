import { authenticatedFetch } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";
import type { HostMcpServer } from "@/hooks/useHarnessInventory";

export interface McpServerEnabledRequest {
  harness: string;
  server: string;
  plugin?: string;
  source_id?: string;
  enabled: boolean;
}

export async function setMcpServerEnabled(
  hostId: string,
  body: McpServerEnabledRequest,
): Promise<HostMcpServer> {
  const response = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/mcp-servers/enabled`,
    { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
  );
  if (!response.ok)
    throw new ApiError(`${response.status} ${response.statusText}`, response.status, null);
  return ((await response.json()) as { server: HostMcpServer }).server;
}
