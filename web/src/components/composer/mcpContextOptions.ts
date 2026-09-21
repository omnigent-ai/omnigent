import type { McpServerSummary } from "@/hooks/useAgents";

export interface McpContextOption {
  id: string;
  serverName: string;
  description: string | null;
  transport: string;
  detail: string | null;
}

function transportLabel(transport: string): string {
  if (transport === "http") return "HTTP";
  if (transport === "stdio") return "stdio";
  return transport;
}

function serverDetail(server: McpServerSummary): string | null {
  if (server.transport === "http" && server.url) {
    try {
      return new URL(server.url).host;
    } catch {
      return null;
    }
  }
  if (server.transport === "stdio" && server.command) {
    return server.command.split(/[\\/]/).filter(Boolean).at(-1) ?? null;
  }
  return null;
}

export function mcpContextOptionsFromServers(
  servers: readonly McpServerSummary[],
): McpContextOption[] {
  const seen = new Set<string>();
  const options: McpContextOption[] = [];
  for (const server of servers) {
    const serverName = server.name.trim();
    if (!serverName || seen.has(serverName)) continue;
    seen.add(serverName);
    options.push({
      id: serverName,
      serverName,
      description: server.description?.trim() || null,
      transport: transportLabel(server.transport),
      detail: serverDetail(server),
    });
  }
  return options;
}
