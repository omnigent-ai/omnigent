// The office MCP endpoint (Streamable HTTP, JSON responses). Each role
// session authenticates with its own bearer token, which identifies its
// branch and role; tools act only within that identity.

import { COORDINATOR_ROLE, type AgentRoleId, type GateType } from "../shared/sdlcTheme.ts";
import { teamOf, teamRoleIds } from "../shared/team.ts";
import { WorkflowError, type McpCaller, type WorkflowService } from "./workflow.ts";

const PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"];

interface JsonRpcRequest {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
}

type ToolHandler = (caller: McpCaller, args: Record<string, unknown>) => Promise<unknown> | unknown;

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  /** Only the PM may call it; the workflow refuses other roles. */
  coordinatorOnly?: boolean;
  /** The schema for one caller, when it depends on the caller's team (its roles or task types). */
  schemaFor?: (inputSchema: Record<string, unknown>, caller: McpCaller) => Record<string, unknown>;
  handle: ToolHandler;
}

/** The office tools a role may have; the PO can still turn some off (see toolsForCaller). */
export function toolsForRole(tools: ToolDefinition[], role: AgentRoleId): ToolDefinition[] {
  return tools.filter((tool) => !tool.coordinatorOnly || role === COORDINATOR_ROLE);
}

/** The tools a session may use now: its role's tools minus any the PO turned off. */
export function toolsForCaller(tools: ToolDefinition[], caller: McpCaller): ToolDefinition[] {
  const role = caller.link.role;
  const disabled = new Set(caller.branch.runtime?.roleToolSettings?.[role]?.disabledTools ?? []);
  return toolsForRole(tools, role).filter((tool) => !disabled.has(tool.name));
}

const stringArray = { type: "array", items: { type: "string" } };

/** The schema with one string property limited to the given values. */
function withEnum(schema: Record<string, unknown>, property: string, values: string[]): Record<string, unknown> {
  const properties = (schema.properties ?? {}) as Record<string, unknown>;
  return { ...schema, properties: { ...properties, [property]: { type: "string", enum: values } } };
}
const gateTypes: GateType[] = ["requirements", "architecture", "merge", "deployment", "outage_triage"];

const requirementSchema = {
  type: "object",
  properties: {
    id: { type: "string", description: "Keep an existing id to edit that requirement" },
    title: { type: "string" },
    description: { type: "string" },
    acceptanceCriteria: stringArray,
  },
  required: ["title"],
};

export function officeTools(workflow: WorkflowService): ToolDefinition[] {
  return [
    {
      name: "office_board_get",
      description: "Read the requirements board, all tasks with their contracts, gate requests, decisions, and the RACI matrix.",
      inputSchema: { type: "object", properties: {} },
      handle: (caller) => workflow.boardSnapshot(caller),
    },
    {
      name: "office_board_propose",
      coordinatorOnly: true,
      description: "PM only. Propose the full requirements list (with acceptance criteria) for the PO to accept or reject.",
      inputSchema: {
        type: "object",
        properties: { requirements: { type: "array", items: requirementSchema }, rationale: { type: "string" } },
        required: ["requirements"],
      },
      handle: (caller, args) => workflow.proposeBoard(caller, args),
    },
    {
      name: "office_task_create",
      coordinatorOnly: true,
      description:
        "PM only. Create a workflow task. Responsible and accountable roles come from the matrix row (taskType). " +
        "Every task needs a deliverable, acceptance criteria, and the evidence required for completion.",
      inputSchema: {
        type: "object",
        properties: {
          taskType: { type: "string", description: "One of the room's task types (see the matrix in office_board_get)" },
          title: { type: "string" },
          deliverable: { type: "string" },
          acceptanceCriteria: stringArray,
          evidenceRequired: stringArray,
          dependsOn: { ...stringArray, description: "Task ids that must be done first" },
          resources: { ...stringArray, description: "Resources this task owns exclusively, e.g. a file or service" },
        },
        required: ["taskType", "title", "deliverable", "acceptanceCriteria", "evidenceRequired"],
      },
      schemaFor: (schema, caller) => withEnum(schema, "taskType", teamOf(caller.branch.runtime).taskTypes.map((entry) => entry.id)),
      handle: (caller, args) => workflow.createTask(caller, args),
    },
    {
      name: "office_task_dispatch",
      coordinatorOnly: true,
      description: "PM only. Send a task to its responsible roles. Refused while dependencies are not done. temporary=true starts an extra worker for parallel work.",
      inputSchema: {
        type: "object",
        properties: { taskId: { type: "string", description: "Task id or exact task title" }, brief: { type: "string" }, temporary: { type: "boolean" } },
        required: ["taskId"],
      },
      handle: (caller, args) => workflow.dispatchTask(caller, args),
    },
    {
      name: "office_task_update",
      description: "Report progress on a task you are responsible for: in_progress, blocked, or submitted (with evidence).",
      inputSchema: {
        type: "object",
        properties: {
          taskId: { type: "string", description: "Task id or exact task title" },
          status: { type: "string", enum: ["in_progress", "blocked", "submitted"] },
          summary: { type: "string" },
          references: { ...stringArray, description: "Paths, commits, PRs, or test reports" },
        },
        required: ["taskId", "status", "summary"],
      },
      handle: (caller, args) => workflow.updateTask(caller, args),
    },
    {
      name: "office_task_complete",
      description: "Accountable role only. Accept a task as done once its evidence meets the acceptance criteria and any gate is approved.",
      inputSchema: {
        type: "object",
        properties: { taskId: { type: "string", description: "Task id or exact task title" }, summary: { type: "string" }, references: stringArray },
        required: ["taskId", "summary"],
      },
      handle: (caller, args) => workflow.completeTask(caller, args),
    },
    {
      name: "office_gate_request",
      description:
        "Ask the PO to approve a human gate. Merge and deployment gates must name the exact shell command; the runtime denies it until approved. " +
        "Approval satisfies the workflow gate only; runtime policies still apply.",
      inputSchema: {
        type: "object",
        properties: {
          gate: { type: "string", enum: gateTypes },
          taskId: { type: "string", description: "Task id or exact task title" },
          summary: { type: "string" },
          artifactKind: { type: "string", enum: ["board", "file", "commit", "pull_request", "text"] },
          artifactRef: { type: "string", description: "Path, commit SHA, or PR URL under review" },
          artifactDigest: { type: "string", description: "Content hash or commit SHA of the artifact" },
          operation: { type: "string", description: "Exact command the approval authorizes (merge/deployment)" },
          targetEnvironment: { type: "string" },
        },
        required: ["gate", "summary"],
      },
      handle: (caller, args) => workflow.requestGate(caller, args),
    },
    {
      name: "office_message_role",
      description: "Send a message to another role. Specialists may only message the PM; the PM may message any role.",
      inputSchema: {
        type: "object",
        properties: { role: { type: "string", description: "A role id of this room's team" }, text: { type: "string" } },
        required: ["role", "text"],
      },
      schemaFor: (schema, caller) => withEnum(schema, "role", teamRoleIds(teamOf(caller.branch.runtime))),
      handle: (caller, args) => workflow.messageRole(caller, args),
    },
    {
      name: "office_record_decision",
      coordinatorOnly: true,
      description: "PM only. Record a coordinator decision about shared scope, interfaces, sequencing, or commitments and inform affected roles.",
      inputSchema: {
        type: "object",
        properties: { summary: { type: "string" }, affectedRoles: stringArray, requestedBy: { type: "string" } },
        required: ["summary"],
      },
      handle: (caller, args) => workflow.recordDecision(caller, args),
    },
    {
      name: "office_interview_plan",
      coordinatorOnly: true,
      description:
        "PM only. Add questions to the current interview's agenda without asking them. List the whole agenda first; " +
        "questions added later are shown to the PO as newly added follow-ups.",
      inputSchema: { type: "object", properties: { questions: { type: "array", items: { type: "string" } } }, required: ["questions"] },
      handle: (caller, args) => workflow.planInterview(caller, args),
    },
    {
      name: "office_interview_ask",
      coordinatorOnly: true,
      description:
        "PM only. Ask the PO one interview question: pass a planned questionId, or question text (an unplanned question " +
        "is added as a follow-up). Refused while a previous question is unanswered.",
      inputSchema: { type: "object", properties: { questionId: { type: "string" }, question: { type: "string" } } },
      handle: (caller, args) => workflow.askInterview(caller, args),
    },
    {
      name: "office_interview_skip",
      coordinatorOnly: true,
      description: "PM only. Drop a planned question that earlier answers made unnecessary.",
      inputSchema: { type: "object", properties: { questionId: { type: "string" }, reason: { type: "string" } }, required: ["questionId", "reason"] },
      handle: (caller, args) => workflow.skipInterview(caller, args),
    },
    {
      name: "office_link_publish",
      description:
        "Share a result with the PO as a link on the room's project bar: a deployment or preview URL, a pull request, a report. " +
        "Publishing the same url again updates its label.",
      inputSchema: {
        type: "object",
        properties: {
          label: { type: "string", description: "Short name, e.g. \"Staging app\" or \"PR #42\"" },
          url: { type: "string", description: "Full http(s) URL" },
          description: { type: "string" },
        },
        required: ["label", "url"],
      },
      handle: (caller, args) => workflow.publishLink(caller, args),
    },
    {
      name: "office_post_update",
      description: "Post an informational update to the room's event feed (for Informed roles and the PO).",
      inputSchema: { type: "object", properties: { summary: { type: "string" }, taskId: { type: "string", description: "Task id or exact task title" } }, required: ["summary"] },
      handle: (caller, args) => workflow.postUpdate(caller, args),
    },
  ];
}

function rpcResult(id: JsonRpcRequest["id"], result: unknown) {
  return { jsonrpc: "2.0", id: id ?? null, result };
}

function rpcError(id: JsonRpcRequest["id"], code: number, message: string) {
  return { jsonrpc: "2.0", id: id ?? null, error: { code, message } };
}

/**
 * Handle one JSON-RPC message (or batch). Returns null when every message
 * was a notification, which the transport answers with 202.
 */
export async function handleMcpMessage(
  payload: unknown,
  caller: McpCaller,
  tools: ToolDefinition[],
): Promise<unknown> {
  const messages = Array.isArray(payload) ? payload : [payload];
  const responses: unknown[] = [];
  for (const raw of messages) {
    const message = (raw ?? {}) as JsonRpcRequest;
    const isNotification = message.id === undefined;
    const response = await handleOne(message, caller, tools);
    if (!isNotification && response) responses.push(response);
  }
  if (responses.length === 0) return null;
  return Array.isArray(payload) ? responses : responses[0];
}

async function handleOne(message: JsonRpcRequest, caller: McpCaller, tools: ToolDefinition[]): Promise<unknown> {
  switch (message.method) {
    case "initialize": {
      const requested = String(message.params?.protocolVersion ?? "");
      return rpcResult(message.id, {
        protocolVersion: PROTOCOL_VERSIONS.includes(requested) ? requested : PROTOCOL_VERSIONS[0],
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "omnigent-office", version: "0.1.0" },
        instructions: "Workflow tools for this SDLC project room. Your identity and role come from your session.",
      });
    }
    case "ping":
      return rpcResult(message.id, {});
    case "tools/list":
      return rpcResult(message.id, {
        tools: toolsForCaller(tools, caller).map((tool) => ({
          name: tool.name,
          description: tool.description,
          inputSchema: tool.schemaFor ? tool.schemaFor(tool.inputSchema, caller) : tool.inputSchema,
        })),
      });
    case "tools/call": {
      const name = message.params?.name;
      const tool = tools.find((entry) => entry.name === name);
      if (!tool) return rpcError(message.id, -32602, `Unknown tool ${String(name)}`);
      if (!toolsForCaller(tools, caller).includes(tool)) {
        return rpcError(message.id, -32602, `${tool.name} is not available to your role in this room.`);
      }
      const args = (message.params?.arguments ?? {}) as Record<string, unknown>;
      try {
        const result = await tool.handle(caller, args);
        return rpcResult(message.id, { content: [{ type: "text", text: JSON.stringify(result, null, 2) }], isError: false });
      } catch (error) {
        const detail = error instanceof WorkflowError ? `${error.code}: ${error.message}` : (error as Error).message;
        return rpcResult(message.id, { content: [{ type: "text", text: detail }], isError: true });
      }
    }
    default:
      if (message.method?.startsWith("notifications/")) return null;
      return rpcError(message.id, -32601, `Method not found: ${String(message.method)}`);
  }
}
