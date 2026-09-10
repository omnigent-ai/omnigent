import { describe, expect, it } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import {
  isAcpHarnessAgent,
  partitionAgentsByKind,
  selectableSessionAgents,
} from "@/lib/agentGrouping";

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

describe("selectableSessionAgents", () => {
  it("drops the hidden agents every session-creation surface must exclude", () => {
    // Shared by the composer picker AND project settings — filter parity is
    // what stops a project from pinning a default the composer can't show.
    const result = selectableSessionAgents([
      agent({ name: "hello" }),
      agent({ name: "nessie" }),
      agent({ name: "kimi" }),
      agent({ name: "kimi-code" }),
    ]);
    expect(result.map((a) => a.name)).toEqual(["hello"]);
  });
});

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

describe("isAcpHarnessAgent", () => {
  it("trusts the server's acpHarness flag, so an unknown ACP harness still groups right", () => {
    // The whole point of the catalog-derived flag: a builtin ACP row added to
    // ACP_CLI_HARNESSES with an id this frontend has never seen must land under
    // "Harnesses" without any frontend change.
    expect(isAcpHarnessAgent({ harness: "some-future-acp-cli", acpHarness: true })).toBe(true);
    // And the server's "no" wins over the id heuristic.
    expect(isAcpHarnessAgent({ harness: "acp:legacy", acpHarness: false })).toBe(false);
  });

  it("recognizes configured acp:<slug> agents and builtin ACP CLI harnesses", () => {
    // NewChatDialog routes these into the "Harnesses" group (beside the native
    // CLIs), not "Agents" — they select a harness to run, not a composed agent.
    expect(isAcpHarnessAgent({ harness: "acp:devin" })).toBe(true);
    expect(isAcpHarnessAgent({ harness: "acp:kilocode" })).toBe(true);
    expect(isAcpHarnessAgent({ harness: "grok" })).toBe(true);
    // Bare builtin ACP CLI ids: a seeded `devin` agent carries
    // `harness: "devin"`, not `acp:devin`. This is the older-server fallback
    // path (no `acpHarness` flag), which the legacy id set still covers.
    expect(isAcpHarnessAgent({ harness: "devin" })).toBe(true);
  });

  it("does not misclassify composed agents, native harnesses, or missing harness", () => {
    expect(isAcpHarnessAgent({ harness: "claude-sdk" })).toBe(false); // Polly / Debby
    expect(isAcpHarnessAgent({ harness: "claude-native" })).toBe(false); // native harness
    expect(isAcpHarnessAgent({ harness: null })).toBe(false);
    expect(isAcpHarnessAgent(null)).toBe(false);
  });
});
