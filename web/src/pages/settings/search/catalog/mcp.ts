import { settingsEntry } from "./types";

export const MCP_SERVICES_ENTRY = settingsEntry({
  id: "mcp-services",
  available: (availability) => availability.mcpServices,
  section: "mcp",
  label: "Managed MCP services",
  description:
    "Connect once, then add these tools to any session. Your accounts are reused across sessions on local machines, remote hosts, and sandboxes in this workspace.",
  keywords: ["mcp", "tools", "services", "connect", "token", "oauth", "accounts"],
  // The server's services: each card's own actions, by name; none of their data is searchable.
  regions: {
    services: {
      rowActions: {
        connect: "Connect",
        updateToken: "Update token",
        reconnect: "Reconnect",
        test: "Test connection",
        disconnect: "Disconnect",
        saveToken: "Save token",
        manageAccount: "Manage account in Sandbox Integrations",
      },
      rowFields: { token: { before: "Token for " } },
    },
  },
});

export const MCP_SETTINGS = [MCP_SERVICES_ENTRY] as const;
