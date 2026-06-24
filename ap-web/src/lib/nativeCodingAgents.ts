import type { AvailableAgent } from "@/hooks/useAvailableAgents";

export const WRAPPER_LABEL_KEY = "omnigent.wrapper";
export const UI_MODE_LABEL_KEY = "omnigent.ui";
export const UI_MODE_TERMINAL_VALUE = "terminal";

export type NativeCodingAgentIconKind =
  | "claude"
  | "codex"
  | "opencode"
  | "pi"
  | "cursor"
  | "goose"
  | "antigravity"
  | "qwen";
export type NativeCodingAgentCapability = "permissionMode" | "approvalMode";

export interface NativeCodingAgentSpec {
  key: NativeCodingAgentIconKind;
  agentName: string;
  harness: string;
  wrapperLabel: string;
  displayName: string;
  iconKind: NativeCodingAgentIconKind;
  sortRank: number;
  capabilities?: readonly NativeCodingAgentCapability[];
}

export const NATIVE_CODING_AGENTS = [
  {
    key: "claude",
    agentName: "claude-native-ui",
    harness: "claude-native",
    wrapperLabel: "claude-code-native-ui",
    displayName: "Claude Code",
    iconKind: "claude",
    sortRank: 10,
    capabilities: ["permissionMode"],
  },
  {
    key: "codex",
    agentName: "codex-native-ui",
    harness: "codex-native",
    wrapperLabel: "codex-native-ui",
    displayName: "Codex",
    iconKind: "codex",
    sortRank: 20,
    capabilities: ["approvalMode"],
  },
  {
    key: "opencode",
    agentName: "opencode-native-ui",
    harness: "opencode-native",
    wrapperLabel: "opencode-native-ui",
    displayName: "OpenCode",
    iconKind: "opencode",
    sortRank: 25,
    capabilities: ["approvalMode"],
  },
  {
    key: "cursor",
    agentName: "cursor-native-ui",
    harness: "cursor-native",
    wrapperLabel: "cursor-native-ui",
    displayName: "Cursor",
    iconKind: "cursor",
    sortRank: 30,
  },
  {
    key: "pi",
    agentName: "pi-native-ui",
    harness: "pi-native",
    wrapperLabel: "pi-native-ui",
    displayName: "Pi",
    iconKind: "pi",
    sortRank: 40,
  },
  {
    // Antigravity's native CLI (Gemini-family). Mirrors the server's
    // canonical `antigravity-native` harness and the `antigravity-native-ui`
    // wrapper the runner keys off to boot the terminal. Added ALONGSIDE the
    // upstream in-process `antigravity` SDK harness (see BRAIN_HARNESS_LABELS
    // in agentLabels.ts) — they are distinct rows.
    key: "antigravity",
    agentName: "antigravity-native-ui",
    harness: "antigravity-native",
    wrapperLabel: "antigravity-native-ui",
    displayName: "Antigravity",
    iconKind: "antigravity",
    sortRank: 45,
  },
  {
    key: "goose",
    agentName: "goose-native-ui",
    harness: "goose-native",
    wrapperLabel: "goose-native-ui",
    displayName: "Goose",
    iconKind: "goose",
    sortRank: 50,
  },
  {
    // qwen has no brand glyph yet, so it falls back to the generic bot icon
    // (see AgentCard.iconForAgent / SubagentsPanel) — the `iconKind: "qwen"`
    // intentionally matches no icon branch. Auth/approval surface in the
    // embedded terminal, so no capability flags are declared here.
    key: "qwen",
    agentName: "qwen-native-ui",
    harness: "qwen-native",
    wrapperLabel: "qwen-native-ui",
    displayName: "Qwen Code",
    iconKind: "qwen",
    sortRank: 60,
  },
] as const satisfies readonly NativeCodingAgentSpec[];

const BY_AGENT_NAME: Map<string, NativeCodingAgentSpec> = new Map(
  NATIVE_CODING_AGENTS.map((agent) => [agent.agentName, agent]),
);
const BY_HARNESS: Map<string, NativeCodingAgentSpec> = new Map(
  NATIVE_CODING_AGENTS.map((agent) => [agent.harness, agent]),
);
const BY_WRAPPER: Map<string, NativeCodingAgentSpec> = new Map(
  NATIVE_CODING_AGENTS.map((agent) => [agent.wrapperLabel, agent]),
);

// Reversed harness spellings that fold to a canonical native `harness`.
// Mirrors omnigent.harness_aliases.NATIVE_HARNESSES on the server, which
// accepts both the canonical and reversed native spellings (claude/codex
// only use the canonical form, so they need no reversed entry here).
const HARNESS_ALIASES: Record<string, string> = {
  "native-pi": "pi-native",
  "native-cursor": "cursor-native",
  "native-antigravity": "antigravity-native",
  "native-goose": "goose-native",
  "native-qwen": "qwen-native",
};

export function nativeCodingAgentForAgentName(
  name: string | null | undefined,
): NativeCodingAgentSpec | undefined {
  return name == null ? undefined : BY_AGENT_NAME.get(name);
}

export function nativeCodingAgentForHarness(
  harness: string | null | undefined,
): NativeCodingAgentSpec | undefined {
  if (harness == null) return undefined;
  return BY_HARNESS.get(HARNESS_ALIASES[harness] ?? harness);
}

export function nativeCodingAgentForWrapper(
  wrapper: string | null | undefined,
): NativeCodingAgentSpec | undefined {
  return wrapper == null ? undefined : BY_WRAPPER.get(wrapper);
}

export function nativeCodingAgentForAvailableAgent(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
): NativeCodingAgentSpec | undefined {
  if (agent == null) return undefined;
  return nativeCodingAgentForHarness(agent.harness) ?? nativeCodingAgentForAgentName(agent.name);
}

export function isNativeCodingAgent(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
): boolean {
  return nativeCodingAgentForAvailableAgent(agent) !== undefined;
}

export function isNativeWrapper(wrapper: string | null | undefined): boolean {
  return nativeCodingAgentForWrapper(wrapper) !== undefined;
}

export function nativeWrapperLabelsForAgent(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
): Record<string, string> | undefined {
  const nativeAgent = nativeCodingAgentForAvailableAgent(agent);
  if (nativeAgent === undefined) return undefined;
  return {
    [UI_MODE_LABEL_KEY]: UI_MODE_TERMINAL_VALUE,
    [WRAPPER_LABEL_KEY]: nativeAgent.wrapperLabel,
  };
}

export function nativeDisplayNameForAgent(agent: Pick<AvailableAgent, "name" | "harness">): string {
  return (
    nativeCodingAgentForAvailableAgent(agent)?.displayName ??
    nativeCodingAgentForAgentName(agent.name)?.displayName ??
    agent.name
  );
}

export function nativeAgentSortRank(agent: Pick<AvailableAgent, "name" | "harness">): number {
  return nativeCodingAgentForAvailableAgent(agent)?.sortRank ?? Number.POSITIVE_INFINITY;
}

export function nativeAgentHasCapability(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
  capability: NativeCodingAgentCapability,
): boolean {
  return nativeCodingAgentForAvailableAgent(agent)?.capabilities?.includes(capability) ?? false;
}
