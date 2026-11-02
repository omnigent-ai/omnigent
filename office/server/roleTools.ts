// Builds the Team tab's tool report from Omnigent's view of a role session,
// or from the agent spec the office would start the role with.

import { type ConfiguredMcpServer, type RoleToolReport, type RoleToolSettings, requiredTools } from "../shared/roleTools.ts";
import type { AgentRoleId } from "../shared/sdlcTheme.ts";
import type { ToolDefinition } from "./mcp.ts";

export interface RoleToolContext {
  sessionId: string | null;
  /** Epoch seconds the role's session started, to spot settings it predates. */
  sessionStartedAt: number | null;
  settings: RoleToolSettings;
  /** Office gate policies on the session, or those a new session would get. */
  gatePolicies: string[];
  /** The agent spec a new session for the role would use. */
  plannedSpec: Record<string, unknown>;
}

const GATE_POLICY_DESCRIPTION = "Office gate: denies merge or deploy commands until the PO approves that exact operation.";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function serverFromAgent(entry: unknown): ConfiguredMcpServer | null {
  const server = record(entry);
  const name = text(server.name);
  if (!name) return null;
  const command = text(server.command);
  const args = Array.isArray(server.args) ? server.args.filter((arg): arg is string => typeof arg === "string") : [];
  return { name, transport: text(server.transport), target: text(server.url) ?? (command ? [command, ...args].join(" ") : null) };
}

/** Configuration as Omnigent reports it for a running session (`GET /v1/sessions/{id}/agent`). */
function fromSession(agent: unknown) {
  const body = record(agent);
  const list = (value: unknown) => (Array.isArray(value) ? value : []);
  return {
    mcpServers: list(body.mcp_servers).map(serverFromAgent).filter((server): server is ConfiguredMcpServer => server !== null),
    skills: list(body.skills)
      .map((skill) => text(skill) ?? text(record(skill).name))
      .filter((name): name is string => name !== null),
    policies: list(body.policies)
      .map((policy) => ({ name: text(record(policy).name), description: text(record(policy).description) }))
      .filter((policy): policy is { name: string; description: string | null } => policy.name !== null),
  };
}

/** Configuration from the agent spec the office launches the role with. */
function fromSpec(spec: Record<string, unknown>) {
  const tools = Object.entries(record(spec.tools))
    .filter(([, tool]) => record(tool).type === "mcp")
    .map(([name, tool]) => ({ name, transport: "http", target: text(record(tool).url) }));
  const policies = Object.keys(record(record(spec.guardrails).policies)).map((name) => ({ name, description: null }));
  return { mcpServers: tools, skills: [] as string[], policies };
}

export function roleToolReport(
  role: AgentRoleId,
  context: RoleToolContext,
  agent: unknown,
  /** The office tools the role may have, before the PO's choices. */
  roleTools: ToolDefinition[],
): RoleToolReport {
  const source = !context.sessionId ? "planned" : agent ? "session" : "unavailable";
  const configured = source === "session" ? fromSession(agent) : fromSpec(context.plannedSpec);
  const gates = context.gatePolicies.map((name) => ({ name, description: GATE_POLICY_DESCRIPTION }));
  const disabled = new Set(context.settings.disabledTools ?? []);
  const required = requiredTools(role);
  return {
    role,
    sessionId: context.sessionId,
    source,
    ...configured,
    policies: [...configured.policies, ...gates.filter((gate) => !configured.policies.some((policy) => policy.name === gate.name))],
    officeTools: roleTools.filter((tool) => !disabled.has(tool.name)).map(({ name, description }) => ({ name, description })),
    settings: context.settings,
    allOfficeTools: roleTools.map(({ name, description }) => ({ name, description, required: required.includes(name), enabled: !disabled.has(name) })),
    restartPending: Boolean(
      context.sessionStartedAt !== null && context.settings.updatedAt && context.settings.updatedAt > context.sessionStartedAt,
    ),
  };
}
