import { describe, expect, it } from "vitest";
import { handleMcpMessage, officeTools } from "./mcp.ts";
import { WorkflowError, type McpCaller, type WorkflowService } from "./workflow.ts";

const caller = { link: { role: "pm" }, branch: { id: "branch_1" } } as unknown as McpCaller;

const workflow = {
  boardSnapshot: () => ({ board: { revision: 3 } }),
  createTask: async () => {
    throw new WorkflowError(409, "resource_owned", "Resource is owned");
  },
} as unknown as WorkflowService;

const tools = officeTools(workflow);

describe("handleMcpMessage", () => {
  it("negotiates the protocol and lists the office tools", async () => {
    const init = (await handleMcpMessage(
      { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26" } },
      caller,
      tools,
    )) as { result: { protocolVersion: string; capabilities: unknown } };
    expect(init.result.protocolVersion).toBe("2025-03-26");
    expect(init.result.capabilities).toEqual({ tools: { listChanged: false } });

    const list = (await handleMcpMessage({ jsonrpc: "2.0", id: 2, method: "tools/list" }, caller, tools)) as {
      result: { tools: { name: string }[] };
    };
    expect(list.result.tools.map((tool) => tool.name)).toContain("office_gate_request");
  });

  it("answers notifications with no body", async () => {
    expect(await handleMcpMessage({ jsonrpc: "2.0", method: "notifications/initialized" }, caller, tools)).toBeNull();
  });

  it("returns tool results and reports workflow refusals as tool errors", async () => {
    const ok = (await handleMcpMessage(
      { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "office_board_get", arguments: {} } },
      caller,
      tools,
    )) as { result: { content: { text: string }[]; isError: boolean } };
    expect(ok.result.isError).toBe(false);
    expect(JSON.parse(ok.result.content[0].text)).toEqual({ board: { revision: 3 } });

    const refused = (await handleMcpMessage(
      { jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "office_task_create", arguments: {} } },
      caller,
      tools,
    )) as { result: { content: { text: string }[]; isError: boolean } };
    expect(refused.result).toEqual({ content: [{ type: "text", text: "resource_owned: Resource is owned" }], isError: true });
  });

  it("rejects unknown methods and tools", async () => {
    const unknownMethod = (await handleMcpMessage({ jsonrpc: "2.0", id: 5, method: "resources/list" }, caller, tools)) as {
      error: { code: number };
    };
    expect(unknownMethod.error.code).toBe(-32601);
    const unknownTool = (await handleMcpMessage(
      { jsonrpc: "2.0", id: 6, method: "tools/call", params: { name: "sys_os_shell" } },
      caller,
      tools,
    )) as { error: { code: number } };
    expect(unknownTool.error.code).toBe(-32602);
  });
});
