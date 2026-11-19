import { settingsEntry } from "./types";

export const GITHUB_CONNECTION_ENTRY = settingsEntry({
  id: "github-connection",
  available: (availability) => availability.integrationProviders.github,
  section: "integrations",
  label: "GitHub",
  description:
    "Connect GitHub to use your gh and git credentials and add your public SSH keys in new sandboxes.",
  keywords: ["github", "gh", "ssh keys", "credentials", "sandbox", "connect"],
  parts: {
    connect: "Connect GitHub",
    disconnect: "Disconnect",
    manageInstallation: "Manage installation",
  },
  // While the status is checking or couldn't be read, the row shows that instead of parts.
  presence: {
    connect: { states: ["not connected"] },
    disconnect: { states: ["connected"] },
    manageInstallation: { states: ["install link"] },
  },
});

export const DATABRICKS_CONNECTION_ENTRY = settingsEntry({
  id: "databricks-connection",
  available: (availability) => availability.integrationProviders.databricks,
  section: "integrations",
  label: "Databricks",
  description:
    "Connect a Databricks workspace to access Unity Gateway (MCP and model serving) as you in new sandboxes.",
  keywords: ["databricks", "workspace", "unity gateway", "mcp", "model serving", "connect"],
  parts: {
    workspace: "Databricks workspace host",
    connect: "Connect Databricks",
    disconnect: "Disconnect",
  },
  presence: {
    workspace: { states: ["not connected"] },
    connect: { states: ["not connected"] },
    disconnect: { states: ["connected"] },
  },
});

export const INTEGRATION_SETTINGS = [GITHUB_CONNECTION_ENTRY, DATABRICKS_CONNECTION_ENTRY] as const;
