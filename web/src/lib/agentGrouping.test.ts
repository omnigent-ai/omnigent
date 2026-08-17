import { describe, expect, it } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { partitionAgentsByKind } from "@/lib/agentGrouping";

function agent(overrides: Partial<AvailableAgent> & Pick<AvailableAgent, "name">): AvailableAgent {
  return {
    id: `id-${overrides.name}`,
    display_name: overrides.name,
    description: null,
    harness: null,
    skills: [],
    ...overrides,
  };
}

describe("partitionAgentsByKind", () => {
  it("groups a server-marked built-in with harnesses even when its name isn't in the static allowlist", () => {
    // A seeded ACP agent (Devin) — name not in BUILTIN_AGENTS, but the server
    // set builtin=true. Without grouping by that flag it would fall to customs.
    const { builtins, customs } = partitionAgentsByKind([
      agent({ name: "devin", harness: "acp:devin", builtin: true }),
      agent({ name: "grok", harness: "grok", builtin: true }),
    ]);
    expect(builtins.map((a) => a.name)).toEqual(expect.arrayContaining(["devin", "grok"]));
    expect(customs).toHaveLength(0);
  });

  it("keeps a server-marked non-built-in in customs", () => {
    const { builtins, customs } = partitionAgentsByKind([
      agent({ name: "my-uploaded-agent", builtin: false }),
    ]);
    expect(builtins).toHaveLength(0);
    expect(customs.map((a) => a.name)).toEqual(["my-uploaded-agent"]);
  });

  it("falls back to the name allowlist when the server omits the builtin field", () => {
    // Older server: no builtin field. The allowlist still classifies the shipped
    // native, and an unknown name is custom.
    const { builtins, customs } = partitionAgentsByKind([
      agent({ name: "claude-native-ui", harness: "claude-native" }),
      agent({ name: "some-custom", harness: "claude-sdk" }),
    ]);
    expect(builtins.map((a) => a.name)).toEqual(["claude-native-ui"]);
    expect(customs.map((a) => a.name)).toEqual(["some-custom"]);
  });
});
