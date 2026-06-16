import type { AvailableAgent } from "@/hooks/useAvailableAgents";

export const WRAPPER_LABEL_KEY = "omnigent.wrapper";
export const UI_MODE_LABEL_KEY = "omnigent.ui";
export const UI_MODE_TERMINAL_VALUE = "terminal";

export type NativeCodingAgentIconKind = "claude" | "codex" | "pi";
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
    key: "pi",
    agentName: "pi-native-ui",
    harness: "pi-native",
    wrapperLabel: "pi-native-ui",
    displayName: "Pi",
    iconKind: "pi",
    sortRank: 30,
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
// Mirrors omnigent.harness_aliases on the server: only `native-pi` is a
// supported reversed alias (claude/codex use the canonical form).
const HARNESS_ALIASES: Record<string, string> = {
  "native-pi": "pi-native",
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

// ── Permission / approval mode constants ────────────────────────────
//
// Shared between the New Chat dialog (create-time picker) and the
// in-session AgentPicker (mid-session switcher). Keep in sync with
// `claude --help` / `codex --help`.

export interface NativeAgentMode {
  value: string;
  label: string;
  description: string;
}

// Claude Code `--permission-mode` choices.
export const CLAUDE_PERMISSION_MODE_DEFAULT = "default";
export const CLAUDE_PERMISSION_MODES: readonly NativeAgentMode[] = [
  { value: "default", label: "Default", description: "Prompts before edits and commands" },
  { value: "auto", label: "Auto", description: "Auto-runs; a classifier blocks risky actions" },
  {
    value: "acceptEdits",
    label: "Accept edits",
    description: "Auto-applies file edits; commands still prompt",
  },
  { value: "plan", label: "Plan", description: "Plans only; makes no edits" },
  { value: "dontAsk", label: "Don't ask", description: "Auto-denies anything not pre-approved" },
  {
    value: "bypassPermissions",
    label: "Bypass permissions",
    description: "Runs everything; no prompts or safety checks",
  },
];

// Codex `--approval-mode` choices.
export const CODEX_APPROVAL_MODE_DEFAULT = "suggest";
export const CODEX_APPROVAL_MODES: readonly NativeAgentMode[] = [
  { value: "suggest", label: "Suggest", description: "Prompts before edits and commands" },
  {
    value: "auto-edit",
    label: "Auto edit",
    description: "Auto-applies file edits; commands still prompt",
  },
  {
    value: "full-auto",
    label: "Full auto",
    description: "Runs everything; no prompts or safety checks",
  },
];

/**
 * Resolve the mode list and CLI flag for a wrapper label.
 *
 * @returns The mode options, default value, and CLI flag name for the
 *   given wrapper, or ``null`` for wrappers without mode support.
 */
export function nativeModeConfigForWrapper(wrapper: string | null | undefined): {
  modes: readonly NativeAgentMode[];
  defaultMode: string;
  cliFlag: string;
  sectionLabel: string;
} | null {
  if (wrapper === "claude-code-native-ui") {
    return {
      modes: CLAUDE_PERMISSION_MODES,
      defaultMode: CLAUDE_PERMISSION_MODE_DEFAULT,
      cliFlag: "--permission-mode",
      sectionLabel: "Permission mode",
    };
  }
  if (wrapper === "codex-native-ui") {
    return {
      modes: CODEX_APPROVAL_MODES,
      defaultMode: CODEX_APPROVAL_MODE_DEFAULT,
      cliFlag: "--approval-mode",
      sectionLabel: "Approval mode",
    };
  }
  return null;
}

/**
 * Parse a permission/approval mode from stored ``terminal_launch_args``.
 *
 * @returns The mode value (e.g. ``"acceptEdits"``, ``"full-auto"``), or
 *   the wrapper's default when no mode flag is found.
 */
export function parseModeFromLaunchArgs(
  wrapper: string | null | undefined,
  args: readonly string[] | null | undefined,
): string | null {
  const config = nativeModeConfigForWrapper(wrapper);
  if (!config) return null;
  if (!args || args.length === 0) return config.defaultMode;
  const idx = args.indexOf(config.cliFlag);
  if (idx === -1 || idx + 1 >= args.length) return config.defaultMode;
  return args[idx + 1];
}
