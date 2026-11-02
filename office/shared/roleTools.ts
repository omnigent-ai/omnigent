// What each SDLC role session is configured with and which office tools it may call.

import type { AgentRoleId } from "./sdlcTheme.ts";

export interface ConfiguredMcpServer {
  name: string;
  transport: string | null;
  /** URL or command the server runs at. */
  target: string | null;
}

export interface RoleToolReport {
  /** The PO's saved choices for this role. */
  settings: RoleToolSettings;
  /** Every office tool the role could have, including ones the PO turned off. */
  allOfficeTools: { name: string; description: string; required: boolean; enabled: boolean }[];
  /** Saved MCP server or policy changes the running session has not picked up yet. */
  restartPending: boolean;
  role: AgentRoleId;
  sessionId: string | null;
  /** "session": read from the running session; "planned": what the role will start with; "unavailable": Omnigent did not answer. */
  source: "session" | "planned" | "unavailable";
  mcpServers: ConfiguredMcpServer[];
  skills: string[];
  policies: { name: string; description: string | null }[];
  /** Office MCP tools this role may call; the harness's own tools come on top. */
  officeTools: { name: string; description: string }[];
}

/** How Omnigent's blast_radius policy treats recoverable but outward commands (push, merge, deploy). */
export type RiskPolicy = "ask" | "deny" | "catastrophic_only";

export const RISK_POLICY_LABELS: Record<RiskPolicy, string> = {
  ask: "Ask before risky commands (default)",
  deny: "Deny risky commands",
  catastrophic_only: "Only block catastrophic commands",
};

/** The PO's tool and policy choices for one role. */
export interface RoleToolSettings {
  /** Office tools the role may not use. */
  disabledTools?: string[];
  /** MCP servers added to the role's session, beside the office's own. */
  mcpServers?: { name: string; url: string }[];
  riskPolicy?: RiskPolicy;
  /** Epoch seconds of the last change, to tell a running session it is out of date. */
  updatedAt?: number;
}

/** Office tools no role can be cut off from, and the PM's own coordination tools. */
export function requiredTools(role: AgentRoleId): string[] {
  const common = ["office_board_get", "office_task_update", "office_message_role"];
  return role === "pm" ? [...common, "office_task_create", "office_task_dispatch"] : common;
}

export const MAX_EXTRA_MCP_SERVERS = 8;

/**
 * Validate the PO's settings for a role against the tools it can have. Returns the
 * settings to store, or why they are not acceptable.
 */
export function parseRoleToolSettings(
  input: unknown,
  role: AgentRoleId,
  availableTools: string[],
): { settings: Omit<RoleToolSettings, "updatedAt"> } | { error: string } {
  const record = (input && typeof input === "object" ? input : {}) as Record<string, unknown>;
  const settings: Omit<RoleToolSettings, "updatedAt"> = {};

  const disabled = Array.isArray(record.disabledTools) ? [...new Set(record.disabledTools.filter((name): name is string => typeof name === "string"))] : [];
  const unknown = disabled.find((name) => !availableTools.includes(name));
  if (unknown) return { error: `${unknown} is not an office tool this role has.` };
  const locked = disabled.find((name) => requiredTools(role).includes(name));
  if (locked) return { error: `${locked} is required for this role and cannot be turned off.` };
  if (disabled.length) settings.disabledTools = disabled.sort();

  const servers = Array.isArray(record.mcpServers) ? record.mcpServers : [];
  if (servers.length > MAX_EXTRA_MCP_SERVERS) return { error: `A role can have at most ${MAX_EXTRA_MCP_SERVERS} extra MCP servers.` };
  const parsed: { name: string; url: string }[] = [];
  for (const entry of servers) {
    const server = (entry && typeof entry === "object" ? entry : {}) as Record<string, unknown>;
    const name = typeof server.name === "string" ? server.name.trim() : "";
    const raw = typeof server.url === "string" ? server.url.trim() : "";
    if (!/^[a-z][a-z0-9_-]{0,39}$/.test(name)) return { error: `MCP server name "${name}" must start with a letter and use lowercase letters, digits, - or _.` };
    if (name === "office") return { error: `"office" is the office's own MCP server.` };
    if (parsed.some((other) => other.name === name)) return { error: `Two MCP servers are named "${name}".` };
    let url: URL;
    try {
      url = new URL(raw);
    } catch {
      return { error: `MCP server "${name}" needs a full http(s) URL.` };
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return { error: `MCP server "${name}" needs an http(s) URL.` };
    parsed.push({ name, url: url.href });
  }
  if (parsed.length) settings.mcpServers = parsed;

  const risk = record.riskPolicy;
  if (risk !== undefined && risk !== "ask" && risk !== "deny" && risk !== "catastrophic_only") return { error: "Unknown risk policy." };
  if (risk && risk !== "ask") settings.riskPolicy = risk;
  return { settings };
}
