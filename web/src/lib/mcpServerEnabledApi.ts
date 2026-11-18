import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import type { HostMcpServer } from "@/hooks/useHarnessInventory";

export interface McpServerEnabledRequest {
  harness: "claude" | "codex";
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
  if (!response.ok) throw await apiErrorFromResponse(response);
  return ((await response.json()) as { server: HostMcpServer }).server;
}
