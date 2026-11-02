// Builds the session-scoped agent bundle (a gzipped tarball holding one
// config.yaml) that Omnigent's bundled session create accepts.

import { gzipSync } from "node:zlib";
import {
  RUNTIME_GATE_PATTERNS,
  RUNTIME_GATE_TOOLS,
  SDLC_WORKFLOW_RULES,
  SHELL_TOOL_NAMES,
  roleAgentName,
  type AgentRoleId,
  type GateType,
} from "../shared/sdlcTheme.ts";
import { teamOf, teamRole, teamRoleTitle } from "../shared/team.ts";
import { effectiveRoleHarness } from "../shared/teamTemplates.ts";
import type { BranchRuntime } from "../shared/workflowTypes.ts";

const BLOCK = 512;

function header(name: string, size: number): Buffer {
  const block = Buffer.alloc(BLOCK);
  block.write(name, 0, 100, "utf8");
  block.write("0000644\0", 100, "ascii");
  block.write("0000000\0", 108, "ascii");
  block.write("0000000\0", 116, "ascii");
  block.write(`${size.toString(8).padStart(11, "0")}\0`, 124, "ascii");
  block.write(`${Math.floor(Date.now() / 1000).toString(8).padStart(11, "0")}\0`, 136, "ascii");
  block.fill(" ", 148, 156);
  block.write("0", 156, "ascii");
  block.write("ustar\0", 257, "ascii");
  block.write("00", 263, "ascii");
  let checksum = 0;
  for (const byte of block) checksum += byte;
  block.write(`${checksum.toString(8).padStart(6, "0")}\0 `, 148, "ascii");
  return block;
}

/** A minimal ustar archive of regular files, gzipped. */
export function tarGzip(files: Record<string, string>): Buffer {
  const parts: Buffer[] = [];
  for (const [name, content] of Object.entries(files)) {
    const data = Buffer.from(content, "utf8");
    parts.push(header(name, data.length), data, Buffer.alloc((BLOCK - (data.length % BLOCK)) % BLOCK));
  }
  parts.push(Buffer.alloc(BLOCK * 2));
  return gzipSync(Buffer.concat(parts));
}

/** Escape a literal for an RE2 pattern inside a CEL string. */
function celString(value: string): string {
  return JSON.stringify(value);
}

/**
 * CEL expression for one runtime gate: deny shell commands (and equivalent
 * tools) that perform the gated operation unless the command is exactly an
 * approved operation. Abstains otherwise so other policies still decide.
 */
export function gatePolicyExpression(gate: GateType, approvedOperations: string[]): string | null {
  const pattern = RUNTIME_GATE_PATTERNS[gate];
  if (!pattern) return null;
  const shellTools = `[${SHELL_TOOL_NAMES.map(celString).join(", ")}]`;
  const approved = `[${approvedOperations.map((operation) => celString(operation.trim())).join(", ")}]`;
  const reason = celString(
    `The ${gate} gate has not been approved for this exact operation. Request it with office_gate_request and wait for the PO.`,
  );
  const shellMatch =
    `event.data.name in ${shellTools} && has(event.data.arguments.command) && ` +
    `event.data.arguments.command.matches(${celString(pattern)}) && ` +
    `!(event.data.arguments.command in ${approved})`;
  const tools = RUNTIME_GATE_TOOLS[gate] ?? [];
  const toolMatch = tools.length && approvedOperations.length === 0
    ? ` || ${tools.map((tool) => `event.data.name.endsWith(${celString(tool)})`).join(" || ")}`
    : "";
  return `event.type == "tool_call" && ((${shellMatch})${toolMatch}) ? {"result": "DENY", "reason": ${reason}} : {}`;
}

export interface RoleBundleOptions {
  role: AgentRoleId;
  projectName: string;
  runtime: BranchRuntime;
  mcpUrl: string;
  mcpToken: string;
  temporaryTaskTitle?: string;
}

export function roleInstructions(
  options: Pick<RoleBundleOptions, "role" | "projectName" | "temporaryTaskTitle"> & { runtime?: BranchRuntime },
): string {
  const custom = options.runtime?.roleCustomInstructions?.[options.role]?.trim();
  const team = teamOf(options.runtime);
  const role = teamRole(team, options.role);
  const lines = [
    `Project: ${options.projectName}. Theme: SDLC. Your role: ${teamRoleTitle(team, options.role)}.`,
    role?.brief ?? "",
    `Team: ${team.roles.map((entry) => `${entry.title} (${entry.id})`).join(", ")}; the human Product Owner is the PO (po).`,
    "",
    SDLC_WORKFLOW_RULES,
    "",
    "The office tools (office_*) are your only channel for workflow state, other roles, and the PO.",
  ];
  if (custom) lines.push("", "PO instructions for this role:", custom);
  if (options.temporaryTaskTitle) {
    lines.push(`You are a temporary worker for one task: ${options.temporaryTaskTitle}. Stop when it is submitted.`);
  }
  return lines.join("\n");
}

export const roleHarness = effectiveRoleHarness;

export function roleAgentSpec(options: RoleBundleOptions): Record<string, unknown> {
  const executor: Record<string, unknown> = { config: { harness: roleHarness(options.runtime, options.role) } };
  const model = options.runtime.roleModels?.[options.role] ?? options.runtime.model;
  if (model) executor.model = model;
  const effort = options.runtime.roleEfforts?.[options.role];
  if (effort) executor.reasoning_effort = effort;
  const settings = options.runtime.roleToolSettings?.[options.role];
  const extraServers = Object.fromEntries((settings?.mcpServers ?? []).map((server) => [server.name, { type: "mcp", url: server.url }]));
  const risk = settings?.riskPolicy ?? "ask";
  return {
    spec_version: 1,
    name: roleAgentName(options.role),
    description: `SDLC ${teamRoleTitle(teamOf(options.runtime), options.role)} for ${options.projectName}`,
    prompt: roleInstructions(options),
    executor,
    os_env: { type: "caller_process", cwd: ".", sandbox: { type: "none" } },
    tools: {
      ...extraServers,
      office: {
        type: "mcp",
        url: options.mcpUrl,
        headers: { Authorization: `Bearer ${options.mcpToken}` },
      },
    },
    guardrails: {
      policies: {
        // Runtime authority stays with Omnigent: by default outward commands ask; the PO
        // may deny them instead, or keep only the catastrophic-command block.
        blast_radius: {
          type: "function",
          function: {
            path: "omnigent.inner.nessie.policies.blast_radius",
            arguments: risk === "catastrophic_only" ? { gate_pushes: false } : { gate_pushes: true, risky_action: risk === "deny" ? "DENY" : "ASK" },
          },
        },
      },
    },
  };
}

export function roleBundle(options: RoleBundleOptions): Buffer {
  // JSON is valid YAML, which keeps the bundle free of a YAML dependency.
  return tarGzip({ "config.yaml": `${JSON.stringify(roleAgentSpec(options), null, 2)}\n` });
}
