import { gunzipSync } from "node:zlib";
import { describe, expect, it } from "vitest";
import { AGENT_ROLES, type AgentRoleId } from "../shared/sdlcTheme.ts";
import type { BranchRuntime } from "../shared/workflowTypes.ts";
import { gatePolicyExpression, roleAgentSpec, tarGzip } from "./bundle.ts";

describe("tarGzip", () => {
  it("writes a ustar archive with a valid header checksum", () => {
    const tar = gunzipSync(tarGzip({ "config.yaml": "name: x\n" }));
    expect(tar.subarray(0, 11).toString("utf8")).toBe("config.yaml");
    expect(tar.subarray(257, 262).toString("ascii")).toBe("ustar");
    const stored = parseInt(tar.subarray(148, 154).toString("ascii"), 8);
    const header = Buffer.from(tar.subarray(0, 512));
    header.fill(" ", 148, 156);
    expect(header.reduce((sum, byte) => sum + byte, 0)).toBe(stored);
    expect(tar.subarray(512, 520).toString("utf8")).toBe("name: x\n");
    expect(tar.length % 512).toBe(0);
  });
});

describe("roleAgentSpec", () => {
  it("defaults each role to its own harness unless overridden", () => {
    const harnessOf = (role: AgentRoleId, runtime: BranchRuntime = { runnerId: "r" }) =>
      (roleAgentSpec({ role, projectName: "Shop", runtime, mcpUrl: "http://x", mcpToken: "t" }).executor as { config: { harness: string } }).config.harness;
    expect(AGENT_ROLES.map((role) => harnessOf(role))).toEqual(["codex", "codex", "claude-sdk", "copilot", "claude-sdk", "agy"]);
    expect(harnessOf("pm", { harness: "claude-sdk", runnerId: "r" })).toBe("claude-sdk");
    expect(harnessOf("pm", { harness: "claude-sdk", roleHarnesses: { pm: "copilot" }, runnerId: "r" })).toBe("copilot");
  });

  it("gives each role its brief, the office MCP server, and the runtime blast-radius policy", () => {
    const spec = roleAgentSpec({
      role: "devops",
      projectName: "Shop",
      runtime: { harness: "claude-sdk", model: "base", roleModels: { devops: "ops-model" }, runnerId: "r" },
      mcpUrl: "http://127.0.0.1:6780/office/mcp",
      mcpToken: "secret",
    });
    expect(spec.prompt).toContain("You are DevOps");
    expect(spec.executor).toEqual({ config: { harness: "claude-sdk" }, model: "ops-model" });
    expect(spec.tools).toEqual({
      office: { type: "mcp", url: "http://127.0.0.1:6780/office/mcp", headers: { Authorization: "Bearer secret" } },
    });
    expect(JSON.stringify(spec.guardrails)).toContain("blast_radius");
  });
});

describe("gatePolicyExpression", () => {
  it("denies gated commands except exactly approved operations, and abstains otherwise", () => {
    const expression = gatePolicyExpression("merge", ["gh pr merge 42 --squash"])!;
    expect(expression).toContain('event.data.arguments.command.matches(');
    expect(expression).toContain('!(event.data.arguments.command in ["gh pr merge 42 --squash"])');
    expect(expression).toMatch(/\? \{"result": "DENY", "reason": ".+"\} : \{\}$/);
    // With no approval, merge tools outside the shell are denied too.
    expect(gatePolicyExpression("merge", [])).toContain('event.data.name.endsWith("merge_pull_request")');
  });

  it("has no runtime pattern for document gates", () => {
    expect(gatePolicyExpression("architecture", [])).toBeNull();
  });
});
