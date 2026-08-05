import { describe, expect, it } from "vitest";

import { getMCPServerPreset, groupMCPServerPresets, MCP_SERVER_PRESETS } from "./mcpPresets";

describe("MCP server presets", () => {
  it("is provider-neutral and groups integrations by capability", () => {
    expect(MCP_SERVER_PRESETS.length).toBeGreaterThanOrEqual(14);
    expect(new Set(MCP_SERVER_PRESETS.map((preset) => preset.provider))).toEqual(
      new Set(["AWS", "Cloudflare", "Devin", "Exa", "HashiCorp", "Microsoft", "Upstash"]),
    );
    expect(
      groupMCPServerPresets([...MCP_SERVER_PRESETS].reverse()).map(([category]) => category),
    ).toEqual([
      "Documentation",
      "Web and research",
      "Browser automation",
      "Cloud infrastructure",
      "Developer tools",
      "Data and analytics",
    ]);
  });

  it("only exposes integrations that need no stored secret", () => {
    expect(new Set(MCP_SERVER_PRESETS.map((preset) => preset.auth))).toEqual(
      new Set(["ambient", "none"]),
    );
  });

  it("creates public documentation servers without credentials", () => {
    expect(getMCPServerPreset("microsoft-learn")?.create({})).toEqual({
      name: "microsoft-learn",
      transport: "http",
      url: "https://learn.microsoft.com/api/mcp",
    });
    expect(getMCPServerPreset("aws-knowledge")?.create({})).toEqual({
      name: "aws-knowledge",
      transport: "http",
      url: "https://knowledge-mcp.global.api.aws",
    });
  });

  it("creates a read-only Azure server using the local credential chain", () => {
    expect(getMCPServerPreset("azure")?.create({})).toEqual({
      name: "azure",
      transport: "stdio",
      command: "npx",
      args: ["-y", "@azure/mcp@latest", "server", "start", "--mode", "single", "--read-only"],
    });
  });

  it("creates a secret-free Azure DevOps configuration from declared fields", () => {
    const preset = getMCPServerPreset("azure-devops");
    expect(preset?.fields).toEqual([
      { id: "organization", label: "Organization", placeholder: "contoso", required: true },
    ]);
    expect(preset?.create({ organization: "  contoso  " })).toEqual({
      name: "azure-devops",
      transport: "stdio",
      command: "npx",
      args: ["-y", "@azure-devops/mcp", "contoso", "--authentication", "azcli"],
    });
  });
});
