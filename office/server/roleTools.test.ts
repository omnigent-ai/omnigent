import { describe, expect, it } from "vitest";
import { parseRoleToolSettings } from "../shared/roleTools.ts";
import type { McpCaller } from "./workflow.ts";
import { officeTools, toolsForCaller, toolsForRole } from "./mcp.ts";
import { roleToolReport } from "./roleTools.ts";
import type { WorkflowService } from "./workflow.ts";

const tools = officeTools({} as WorkflowService);
const plannedSpec = {
  tools: { office: { type: "mcp", url: "http://127.0.0.1:6780/office/mcp", headers: {} } },
  guardrails: { policies: { blast_radius: { type: "function" } } },
};

describe("office tools per role", () => {
  it("flags exactly the tools whose description says they are PM only", () => {
    for (const tool of tools) expect(Boolean(tool.coordinatorOnly)).toBe(tool.description.startsWith("PM only."));
  });

  it("gives the PM every tool and specialists the rest", () => {
    expect(toolsForRole(tools, "pm")).toHaveLength(tools.length);
    const developer = toolsForRole(tools, "developer").map((tool) => tool.name);
    expect(developer).toContain("office_task_update");
    expect(developer).not.toContain("office_task_create");
  });
});

describe("roleToolReport", () => {
  it("reads a running session's servers, skills, and policies from Omnigent, adding the office gates", () => {
    const agent = {
      mcp_servers: [
        { name: "office", transport: "http", url: "http://127.0.0.1:6780/office/mcp" },
        { name: "github", transport: "stdio", command: "gh-mcp", args: ["--read-only"] },
        { transport: "http" },
      ],
      skills: [{ name: "deploy" }, "review"],
      policies: [{ name: "blast_radius", description: "Asks before risky commands." }],
    };
    const report = roleToolReport("devops", { sessionId: "conv_1", sessionStartedAt: 100, settings: {}, gatePolicies: ["office_deployment_gate_v2"], plannedSpec }, agent, toolsForRole(tools, "devops"));
    expect(report).toMatchObject({
      source: "session",
      sessionId: "conv_1",
      mcpServers: [
        { name: "office", transport: "http", target: "http://127.0.0.1:6780/office/mcp" },
        { name: "github", transport: "stdio", target: "gh-mcp --read-only" },
      ],
      skills: ["deploy", "review"],
      policies: [{ name: "blast_radius", description: "Asks before risky commands." }, { name: "office_deployment_gate_v2" }],
    });
    expect(report.officeTools.map((tool) => tool.name)).not.toContain("office_interview_ask");
  });

  it("shows what a role will start with, and falls back to it when Omnigent does not answer", () => {
    const planned = roleToolReport("qa", { sessionId: null, sessionStartedAt: null, settings: {}, gatePolicies: ["office_merge_gate"], plannedSpec }, null, []);
    expect(planned).toMatchObject({
      source: "planned",
      mcpServers: [{ name: "office", transport: "http", target: "http://127.0.0.1:6780/office/mcp" }],
      skills: [],
      policies: [{ name: "blast_radius", description: null }, { name: "office_merge_gate" }],
    });
    expect(roleToolReport("qa", { sessionId: "conv_2", sessionStartedAt: 100, settings: {}, gatePolicies: [], plannedSpec }, null, []).source).toBe("unavailable");
  });
});

describe("PO tool and policy choices", () => {
  const developerTools = toolsForRole(tools, "developer").map((tool) => tool.name);

  it("accepts turning off optional tools, extra MCP servers, and a risk policy", () => {
    expect(
      parseRoleToolSettings(
        { disabledTools: ["office_link_publish"], mcpServers: [{ name: "github", url: "https://mcp.example.com/github" }], riskPolicy: "deny" },
        "developer",
        developerTools,
      ),
    ).toEqual({
      settings: { disabledTools: ["office_link_publish"], mcpServers: [{ name: "github", url: "https://mcp.example.com/github" }], riskPolicy: "deny" },
    });
    // The default policy is not stored.
    expect(parseRoleToolSettings({ riskPolicy: "ask" }, "developer", developerTools)).toEqual({ settings: {} });
  });

  it("refuses required tools, tools the role cannot have, and unsafe or clashing servers", () => {
    const refused = (input: unknown, role: "developer" | "pm" = "developer") =>
      (parseRoleToolSettings(input, role, toolsForRole(tools, role).map((tool) => tool.name)) as { error?: string }).error;
    expect(refused({ disabledTools: ["office_task_update"] })).toMatch(/required/);
    expect(refused({ disabledTools: ["office_task_dispatch"] }, "pm")).toMatch(/required/);
    expect(refused({ disabledTools: ["office_task_create"] })).toMatch(/not an office tool this role has/);
    expect(refused({ mcpServers: [{ name: "office", url: "https://x.example" }] })).toMatch(/office's own/);
    expect(refused({ mcpServers: [{ name: "files", url: "file:///etc" }] })).toMatch(/http/);
    expect(refused({ mcpServers: [{ name: "a", url: "https://x.example" }, { name: "a", url: "https://y.example" }] })).toMatch(/Two MCP servers/);
    expect(refused({ riskPolicy: "yolo" })).toMatch(/risk policy/);
  });

  it("hides and refuses tools the PO turned off, for that role only", () => {
    const caller = (role: "developer" | "qa") =>
      ({
        link: { role },
        branch: { runtime: { roleToolSettings: { developer: { disabledTools: ["office_link_publish"] } } } },
      }) as unknown as McpCaller;
    expect(toolsForCaller(tools, caller("developer")).map((tool) => tool.name)).not.toContain("office_link_publish");
    expect(toolsForCaller(tools, caller("qa")).map((tool) => tool.name)).toContain("office_link_publish");
  });

  it("reports the choices and when a running session predates them", () => {
    const settings = { disabledTools: ["office_link_publish"], riskPolicy: "deny" as const, updatedAt: 200 };
    const report = roleToolReport("developer", { sessionId: "conv_1", sessionStartedAt: 100, settings, gatePolicies: [], plannedSpec }, null, toolsForRole(tools, "developer"));
    expect(report.restartPending).toBe(true);
    expect(report.officeTools.map((tool) => tool.name)).not.toContain("office_link_publish");
    expect(report.allOfficeTools.find((tool) => tool.name === "office_link_publish")).toMatchObject({ enabled: false, required: false });
    expect(report.allOfficeTools.find((tool) => tool.name === "office_task_update")).toMatchObject({ enabled: true, required: true });
    expect(roleToolReport("developer", { sessionId: "conv_1", sessionStartedAt: 300, settings, gatePolicies: [], plannedSpec }, null, []).restartPending).toBe(false);
  });
});

describe("office tool schemas per room", () => {
  it("offer each caller its own room's roles and task types", async () => {
    const { addRole, addTaskType, sdlcTeam } = await import("../shared/team.ts");
    const withRole = addRole(sdlcTeam(), "Designer").team;
    const team = addTaskType(withRole, "Design screens").team;
    const caller = { link: { role: "pm" }, branch: { runtime: { team } } } as unknown as McpCaller;
    const schemaOf = (name: string) => {
      const tool = tools.find((entry) => entry.name === name)!;
      return (tool.schemaFor?.(tool.inputSchema, caller).properties ?? {}) as Record<string, { enum?: string[] }>;
    };
    expect(schemaOf("office_message_role").role.enum).toContain("designer");
    expect(schemaOf("office_task_create").taskType.enum).toContain("design_screens");
  });
});
