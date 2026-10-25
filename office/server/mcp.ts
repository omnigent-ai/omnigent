// The office MCP endpoint (Streamable HTTP, JSON responses). Each role
// session authenticates with its own bearer token, which identifies its
// branch and role; tools act only within that identity.

import { SDLC_MATRIX, AGENT_ROLES, type GateType } from "../shared/sdlcTheme.ts";
import { WorkflowError, type McpCaller, type WorkflowService } from "./workflow.ts";

const PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"];

interface JsonRpcRequest {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
}

type ToolHandler = (caller: McpCaller, args: Record<string, unknown>) => Promise<unknown> | unknown;

interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  handle: ToolHandler;
}

const stringArray = { type: "array", items: { type: "string" } };
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
      description:
        "PM only. Create a workflow task. Responsible and accountable roles come from the matrix row (taskType). " +
        "Every task needs a deliverable, acceptance criteria, and the evidence required for completion.",
      inputSchema: {
        type: "object",
        properties: {
          taskType: { type: "string", enum: SDLC_MATRIX.map((entry) => entry.id) },
          title: { type: "string" },
          deliverable: { type: "string" },
          acceptanceCriteria: stringArray,
          evidenceRequired: stringArray,
          dependsOn: { ...stringArray, description: "Task ids that must be done first" },
          resources: { ...stringArray, description: "Resources this task owns exclusively, e.g. a file or service" },
        },
        required: ["taskType", "title", "deliverable", "acceptanceCriteria", "evidenceRequired"],
      },
      handle: (caller, args) => workflow.createTask(caller, args),
    },
    {
      name: "office_task_dispatch",
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
        properties: { role: { type: "string", enum: [...AGENT_ROLES] }, text: { type: "string" } },
        required: ["role", "text"],
      },
      handle: (caller, args) => workflow.messageRole(caller, args),
    },
    {
      name: "office_record_decision",
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
      description:
        "PM only. Add questions to the current interview's agenda without asking them. List the whole agenda first; " +
        "questions added later are shown to the PO as newly added follow-ups.",
      inputSchema: { type: "object", properties: { questions: { type: "array", items: { type: "string" } } }, required: ["questions"] },
      handle: (caller, args) => workflow.planInterview(caller, args),
    },
    {
      name: "office_interview_ask",
      description:
        "PM only. Ask the PO one interview question: pass a planned questionId, or question text (an unplanned question " +
        "is added as a follow-up). Refused while a previous question is unanswered.",
      inputSchema: { type: "object", properties: { questionId: { type: "string" }, question: { type: "string" } } },
      handle: (caller, args) => workflow.askInterview(caller, args),
    },
    {
      name: "office_interview_skip",
      description: "PM only. Drop a planned question that earlier answers made unnecessary.",
      inputSchema: { type: "object", properties: { questionId: { type: "string" }, reason: { type: "string" } }, required: ["questionId", "reason"] },
      handle: (caller, args) => workflow.skipInterview(caller, args),
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
        tools: tools.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })),
      });
    case "tools/call": {
      const name = message.params?.name;
      const tool = tools.find((entry) => entry.name === name);
      if (!tool) return rpcError(message.id, -32602, `Unknown tool ${String(name)}`);
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
