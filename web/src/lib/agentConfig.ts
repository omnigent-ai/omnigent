import type { AgentConfig, AgentConfigPatch, MCPConfigPatch } from "./agentsApi";
import { CLAUDE_NATIVE_PERMISSION_MODES } from "./claudePermissionMode";
import {
  APPROVAL_SKIPPING_PERMISSION_MODES,
  CODEX_NATIVE_APPROVAL_MODES,
  canonicalNativeHarness,
} from "./nativeHarnessModes";

export interface CredentialDraft {
  name: string;
  value: string;
  action: "keep" | "replace" | "remove";
  existing: boolean;
}

export interface MCPDraft {
  key: string;
  name: string;
  existing: boolean;
  editable: boolean;
  removed: boolean;
  transport: string;
  description: string;
  url: string;
  command: string;
  args: string[];
  credentials: CredentialDraft[];
}

export interface AgentConfigDraft {
  description: string;
  instructions: string;
  model: string;
  harness: string;
  /** Default permission mode for the agent's sessions; "" leaves the harness default. */
  permissionMode: string;
  servers: MCPDraft[];
}

export interface AgentPermissionModeOption {
  value: string;
  label: string;
  description: string;
  skipsApproval: boolean;
}

/**
 * Modes the agent editor offers as an agent's default. Mirrors
 * AGENT_DEFAULT_PERMISSION_MODES in omnigent/native/permission_modes.py.
 */
export function agentPermissionModeOptions(harness: string): AgentPermissionModeOption[] {
  const modes =
    canonicalNativeHarness(harness) === "claude-native"
      ? CLAUDE_NATIVE_PERMISSION_MODES
      : canonicalNativeHarness(harness) === "codex-native"
        ? CODEX_NATIVE_APPROVAL_MODES
        : [];
  return modes.map(({ value, label, description }) => ({
    value,
    label,
    description,
    skipsApproval: APPROVAL_SKIPPING_PERMISSION_MODES.has(value),
  }));
}

export function agentConfigDraft(config: AgentConfig): AgentConfigDraft {
  return {
    description: config.description ?? "",
    instructions: config.instructions ?? "",
    model: config.model ?? "",
    harness: config.harness ?? "",
    permissionMode: config.permission_mode ?? "",
    servers: Object.entries(config.mcp_servers).map(([name, server]) => ({
      key: name,
      name,
      existing: true,
      editable: server.editable && ["http", "stdio"].includes(server.transport),
      removed: false,
      transport: server.transport,
      description: server.description ?? "",
      url: server.url ?? "",
      command: server.command ?? "",
      args: [...server.args],
      credentials: (server.transport === "http"
        ? server.configured_header_names
        : server.configured_env_names
      ).map((credentialName) => ({
        name: credentialName,
        value: "",
        action: "keep",
        existing: true,
      })),
    })),
  };
}

/** Omission preserves server values, especially credentials that were never read. */
export function agentConfigPatch(config: AgentConfig, draft: AgentConfigDraft): AgentConfigPatch {
  const patch: AgentConfigPatch = {};
  if (draft.description !== (config.description ?? ""))
    patch.description = draft.description || null;
  if (draft.instructions !== (config.instructions ?? "")) patch.instructions = draft.instructions;
  for (const field of ["model", "harness"] as const) {
    if (draft[field] !== (config[field] ?? "")) {
      if (!draft[field].trim())
        throw new Error(`${field === "model" ? "Model" : "Harness"} cannot be empty.`);
      patch[field] = draft[field];
    }
  }
  if (draft.permissionMode !== (config.permission_mode ?? ""))
    patch.permission_mode = draft.permissionMode || null;
  const names = new Set<string>();
  const changes: [string, MCPConfigPatch | null][] = [];
  for (const row of draft.servers) {
    if (names.has(row.name)) throw new Error("MCP server names must be unique.");
    names.add(row.name);
    if (!row.editable) continue;
    if (!/^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$/.test(row.name))
      throw new Error("Enter a valid MCP server name (letters, numbers, _, - or .). ");
    if (row.removed) {
      if (row.existing) changes.push([row.name, null]);
      continue;
    }
    const original = row.existing ? config.mcp_servers[row.name] : undefined;
    const change: MCPConfigPatch = {};
    if (!original) change.transport = row.transport as "http" | "stdio";
    if (row.description !== (original?.description ?? ""))
      change.description = row.description || null;
    const field = row.transport === "http" ? "url" : "command";
    if (!original || row[field] !== (original[field] ?? "")) {
      if (!row[field].trim()) throw new Error(`${row.name}: ${field} is required.`);
      if (field === "url" && !/^https?:\/\//.test(row.url))
        throw new Error(`${row.name}: use an HTTP or HTTPS URL.`);
      change[field] = row[field];
    }
    if (
      row.transport === "stdio" &&
      JSON.stringify(row.args) !== JSON.stringify(original?.args ?? [])
    ) {
      change.args = row.args;
    }
    const credentialNames = new Set<string>();
    const credentials: [string, string | null][] = [];
    for (const credential of row.credentials) {
      if (!credential.name.trim()) throw new Error(`${row.name}: enter a credential name.`);
      if (credentialNames.has(credential.name))
        throw new Error(`${row.name}: credential names must be unique.`);
      credentialNames.add(credential.name);
      if (credential.action === "replace") {
        if (credential.value === "[REDACTED]")
          throw new Error("Enter a credential value, not a redaction placeholder.");
        credentials.push([credential.name, credential.value]);
      } else if (credential.action === "remove" && credential.existing) {
        credentials.push([credential.name, null]);
      }
    }
    if (credentials.length)
      change[row.transport === "http" ? "headers" : "env"] = Object.fromEntries(credentials);
    if (Object.keys(change).length) changes.push([row.name, change]);
  }
  if (changes.length) patch.mcp_servers = Object.fromEntries(changes);
  return patch;
}
