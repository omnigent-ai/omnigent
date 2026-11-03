import { describe, expect, it } from "vitest";
import { agentConfigDraft, agentConfigPatch } from "./agentConfig";
import type { AgentConfig } from "./agentsApi";

const config: AgentConfig = {
  id: "a1",
  name: "research",
  version: 1,
  description: "Original",
  instructions: "  literal\ntext\n",
  model: null,
  harness: "acp:custom",
  mcp_servers: {
    docs: {
      transport: "http",
      description: null,
      url: "https://example.com/mcp",
      command: null,
      args: [],
      configured_header_names: ["Authorization"],
      configured_env_names: [],
      editable: true,
    },
    local: {
      transport: "stdio",
      description: null,
      url: null,
      command: "tool",
      args: ["two words", "", "${ARG}"],
      configured_header_names: [],
      configured_env_names: ["TOKEN"],
      editable: true,
    },
    managed: {
      transport: "registry",
      description: null,
      url: null,
      command: null,
      args: [],
      configured_header_names: [],
      configured_env_names: [],
      editable: false,
    },
  },
};

describe("agent configuration patch", () => {
  it("preserves nulls, unknown harnesses, literal text, arguments and hidden credentials", () => {
    const draft = agentConfigDraft(config);
    expect(agentConfigPatch(config, draft)).toEqual({});
    draft.description = "changed";
    expect(agentConfigPatch(config, draft)).toEqual({ description: "changed" });
    draft.description = config.description!;
    expect(agentConfigPatch(config, draft)).toEqual({});
  });
  it("clears fields explicitly without trimming instructions", () => {
    const draft = agentConfigDraft(config);
    draft.description = "";
    draft.instructions = "";
    expect(agentConfigPatch(config, draft)).toEqual({ description: null, instructions: "" });
    draft.instructions = " \n";
    expect(agentConfigPatch(config, draft).instructions).toBe(" \n");
  });
  it("merges credential replacements and removals without resending other keys", () => {
    const draft = agentConfigDraft(config);
    draft.servers[0].url = "https://example.com/updated";
    expect(agentConfigPatch(config, draft)).toEqual({
      mcp_servers: { docs: { url: "https://example.com/updated" } },
    });
    draft.servers[0].credentials[0].action = "remove";
    draft.servers[1].credentials[0] = {
      name: "TOKEN",
      existing: true,
      action: "replace",
      value: " ${TOKEN} ",
    };
    expect(agentConfigPatch(config, draft).mcp_servers).toEqual({
      docs: { url: "https://example.com/updated", headers: { Authorization: null } },
      local: { env: { TOKEN: " ${TOKEN} " } },
    });
    draft.servers[1].credentials[0].value = "";
    expect(agentConfigPatch(config, draft).mcp_servers?.local).toEqual({ env: { TOKEN: "" } });
  });
  it("handles argument clears, server removal/undo and managed entries", () => {
    const draft = agentConfigDraft(config);
    draft.servers[1].args = [];
    draft.servers[0].removed = true;
    draft.servers[2].removed = true;
    expect(agentConfigPatch(config, draft)).toEqual({
      mcp_servers: { docs: null, local: { args: [] } },
    });
    draft.servers[0].removed = false;
    expect(agentConfigPatch(config, draft)).toEqual({ mcp_servers: { local: { args: [] } } });
  });
  it("creates complete servers and rejects duplicate names, invalid connections and placeholders", () => {
    const draft = agentConfigDraft(config);
    draft.servers.push({
      ...draft.servers[0],
      key: "new",
      name: "new",
      existing: false,
      credentials: [],
    });
    expect(agentConfigPatch(config, draft)).toEqual({
      mcp_servers: { new: { transport: "http", url: "https://example.com/mcp" } },
    });
    draft.servers[3].name = "docs";
    expect(() => agentConfigPatch(config, draft)).toThrow("unique");
    draft.servers.pop();
    draft.servers[0].url = "file:///tmp/no";
    expect(() => agentConfigPatch(config, draft)).toThrow("HTTP");
    draft.servers[0].url = config.mcp_servers.docs.url!;
    draft.servers[0].credentials[0] = {
      name: "Authorization",
      existing: true,
      action: "replace",
      value: "[REDACTED]",
    };
    expect(() => agentConfigPatch(config, draft)).toThrow("placeholder");
  });
  it("allows an unset model until it is edited, and preserves argument boundaries", () => {
    const draft = agentConfigDraft(config);
    draft.model = " ";
    expect(() => agentConfigPatch(config, draft)).toThrow("Model");
    draft.model = "custom-model";
    draft.servers[1].args.push(" space ");
    expect(agentConfigPatch(config, draft)).toEqual({
      model: "custom-model",
      mcp_servers: { local: { args: ["two words", "", "${ARG}", " space "] } },
    });
  });
});
