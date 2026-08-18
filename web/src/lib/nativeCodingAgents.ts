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
  | "kiro"
  | "goose"
  | "qwen"
  | "antigravity"
  | "kimi"
  | "hermes";
export type NativeCodingAgentCapability =
  "permissionMode" | "approvalMode" | "cursorMode" | "skipPermissions";

export interface NativeCodingAgentSpec {
  key: NativeCodingAgentIconKind;
  agentName: string;
  harness: string;
  wrapperLabel: string;
  /**
   * `omnigent.wrapper` value stamped on the children this vendor spawns
   * inside its own CLI (Claude's Task tool, Codex collab threads). Mirrors
   * `NativeCodingAgent.subagent_wrapper_label` on the server. Absent for
   * vendors that don't register sub-agent children.
   */
  subagentWrapperLabel?: string;
  displayName: string;
  iconKind: NativeCodingAgentIconKind;
  sortRank: number;
  capabilities?: readonly NativeCodingAgentCapability[];
  /**
   * A fully supported harness — the integration we maintain and test end to
   * end. Only these lead the picker's primary list; every other harness folds
   * into the "More" group regardless of whether it is configured on the host.
   */
  fullySupported?: boolean;
}

export const NATIVE_CODING_AGENTS = [
  {
    key: "claude",
    agentName: "claude-native-ui",
    harness: "claude-native",
    wrapperLabel: "claude-code-native-ui",
    subagentWrapperLabel: "claude-code-native-ui-subagent",
    displayName: "Claude Code",
    iconKind: "claude",
    sortRank: 10,
    capabilities: ["permissionMode"],
    fullySupported: true,
  },
  {
    key: "codex",
    agentName: "codex-native-ui",
    harness: "codex-native",
    wrapperLabel: "codex-native-ui",
    subagentWrapperLabel: "codex-native-ui-subagent",
    displayName: "Codex",
    iconKind: "codex",
    sortRank: 20,
    capabilities: ["approvalMode"],
    fullySupported: true,
  },
  {
    key: "opencode",
    agentName: "opencode-native-ui",
    harness: "opencode-native",
    wrapperLabel: "opencode-native-ui",
    subagentWrapperLabel: "opencode-native-ui-subagent",
    displayName: "OpenCode",
    iconKind: "opencode",
    sortRank: 25,
    // No capabilities → no permission picker. OpenCode has no claude-style
    // permission-mode surface to mirror: its native modes are the `build`
    // (allow-by-default) and `plan` primary agents, switched at runtime via Tab
    // inside the TUI — and `opencode attach` (how the runner launches it) has
    // no `--agent` flag to preset one anyway. The runner already forces
    // `permission: "ask"` so tools route through the Omnigent policy engine, so
    // a launch-time picker would mirror nothing. (Previously declared Codex's
    // `approvalMode`, whose `--sandbox`/`--ask-for-approval` presets aren't
    // understood by `opencode attach` and crashed the TUI on any non-default
    // pick.)
  },
  {
    key: "cursor",
    agentName: "cursor-native-ui",
    harness: "cursor-native",
    wrapperLabel: "cursor-native-ui",
    displayName: "Cursor",
    iconKind: "cursor",
    sortRank: 30,
    capabilities: ["cursorMode"],
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
    key: "kiro",
    agentName: "kiro-native-ui",
    harness: "kiro-native",
    wrapperLabel: "kiro-native-ui",
    displayName: "Kiro",
    iconKind: "kiro",
    sortRank: 50,
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
    subagentWrapperLabel: "antigravity-native-ui-subagent",
    displayName: "Antigravity",
    iconKind: "antigravity",
    sortRank: 45,
    // agy's only pre-emptive control is the all-or-nothing
    // `--dangerously-skip-permissions`, so it gets a two-value toggle rather
    // than Claude's graded permissionMode selector.
    capabilities: ["skipPermissions"],
  },
  {
    key: "goose",
    agentName: "goose-native-ui",
    harness: "goose-native",
    wrapperLabel: "goose-native-ui",
    displayName: "Goose",
    iconKind: "goose",
    sortRank: 60,
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
  {
    key: "kimi",
    agentName: "kimi-native-ui",
    harness: "kimi-native",
    wrapperLabel: "kimi-native-ui",
    displayName: "Kimi",
    iconKind: "kimi",
    sortRank: 70,
  },
  {
    // hermes has no brand glyph yet, so it falls back to the generic bot icon
    // (see AgentCard.iconForAgent / SubagentsPanel) — the `iconKind: "hermes"`
    // intentionally matches no icon branch. Auth/approval surface in the
    // embedded terminal, so no capability flags are declared here.
    key: "hermes",
    agentName: "hermes-native-ui",
    harness: "hermes-native",
    wrapperLabel: "hermes-native-ui",
    displayName: "Hermes",
    iconKind: "hermes",
    sortRank: 80,
  },
] as const satisfies readonly NativeCodingAgentSpec[];

const BY_AGENT_NAME = new Map<string, NativeCodingAgentSpec>(
  NATIVE_CODING_AGENTS.map((agent) => [agent.agentName, agent]),
);
const BY_HARNESS = new Map<string, NativeCodingAgentSpec>(
  NATIVE_CODING_AGENTS.map((agent) => [agent.harness, agent]),
);
const BY_WRAPPER = new Map<string, NativeCodingAgentSpec>(
  NATIVE_CODING_AGENTS.map((agent) => [agent.wrapperLabel, agent]),
);
// Kept out of BY_WRAPPER: a sub-agent child is NOT a native-terminal session
// (it owns no PTY and takes no input), so `isNativeWrapper` must keep
// returning false for these labels.
const BY_SUBAGENT_WRAPPER = new Map<string, NativeCodingAgentSpec>(
  NATIVE_CODING_AGENTS.flatMap((agent) =>
    "subagentWrapperLabel" in agent ? [[agent.subagentWrapperLabel, agent] as const] : [],
  ),
);

// Reversed harness spellings that fold to a canonical native `harness`.
// Mirrors omnigent.harness_aliases.NATIVE_HARNESSES on the server, which
// accepts both the canonical and reversed native spellings (claude/codex
// only use the canonical form, so they need no reversed entry here).
const HARNESS_ALIASES: Record<string, string> = {
  "native-pi": "pi-native",
  "native-cursor": "cursor-native",
  "native-kiro": "kiro-native",
  "native-antigravity": "antigravity-native",
  "native-goose": "goose-native",
  "native-qwen": "qwen-native",
  "native-kimi": "kimi-native",
  "native-hermes": "hermes-native",
  "native-opencode": "opencode-native",
};

// Vendors whose elicitation wire prefix differs from their registry `key`:
// Antigravity's bridge stamps `agy_native_*`.
const POLICY_NAME_VENDORS: Record<string, string> = { antigravity: "agy" };

// Stamped by the generic native-permission hook when the posting bridge sends
// no name of its own — native provenance with no vendor attached.
const VENDORLESS_NATIVE_POLICY_NAME = "native_permission";

// `<vendor>_native_` → spec, derived from the registry so a new vendor row is
// covered without editing a second list. Mirrors the ids the server bridges
// stamp: `omnigent/server/routes/sessions/routes_hooks.py` (Claude, Cursor,
// generic), `routes/_codex_elicitation.py`, `routes/_antigravity_elicitation.py`,
// and the per-vendor `omnigent/<vendor>_native_permissions.py` hooks.
// `<vendor>_native_` is reserved for those bridges: a user-authored policy in
// that shape reads as provenance and loses its own name in the UI.
const NATIVE_POLICY_PREFIXES: readonly (readonly [string, NativeCodingAgentSpec])[] =
  NATIVE_CODING_AGENTS.map((agent) => [
    `${POLICY_NAME_VENDORS[agent.key] ?? agent.key}_native_`,
    agent,
  ]);

/**
 * Resolve the vendor behind an elicitation's synthetic ``policy_name``.
 *
 * Native permission bridges stamp provenance ids — ``claude_native_permission``,
 * ``codex_native_command_approval``, ``kiro_native_permission`` — rather than a
 * policy anyone wrote, so approval surfaces can name the product that asked
 * instead of leaking the id.
 *
 * @param policyName - ``policy_name`` from the elicitation params.
 * @returns The vendor spec, or undefined for user-authored policy names and for
 *   native stamps whose vendor this build doesn't know.
 */
export function nativeCodingAgentForPolicyName(
  policyName: string,
): NativeCodingAgentSpec | undefined {
  return NATIVE_POLICY_PREFIXES.find(([prefix]) => policyName.startsWith(prefix))?.[1];
}

/**
 * Whether a ``policy_name`` is native provenance rather than a policy someone
 * wrote. True for every ``<vendor>_native_*`` stamp and for the vendor-less
 * ``native_permission`` fallback, so callers can hide both the id and the
 * constant ``phase`` that rides along with it.
 *
 * @param policyName - ``policy_name`` from the elicitation params.
 * @returns True when the name came from a harness-native bridge.
 */
export function isNativePolicyName(policyName: string): boolean {
  return (
    policyName === VENDORLESS_NATIVE_POLICY_NAME ||
    nativeCodingAgentForPolicyName(policyName) !== undefined
  );
}

export function nativeCodingAgentForAgentName(
  name: string | null | undefined,
): NativeCodingAgentSpec | undefined {
  return name == null ? undefined : BY_AGENT_NAME.get(name);
}

/**
 * The synthetic ``policy_name`` a native agent's permission prompts carry.
 *
 * History hydration rebuilds answered question / plan cards from persisted
 * tool calls, which name the agent rather than the elicitation provenance
 * the live card came with. Minting the id from the same prefix table
 * :func:`nativeCodingAgentForPolicyName` reads keeps both directions on
 * one source of truth, so the rebuilt card names the same vendor.
 *
 * @param name - Agent name from the item, e.g. ``"claude-native-ui"``.
 * @returns The provenance id, or ``""`` for a non-native agent.
 */
export function nativePolicyNameForAgentName(name: string | null | undefined): string {
  const spec = nativeCodingAgentForAgentName(name);
  if (spec === undefined) return "";
  return `${POLICY_NAME_VENDORS[spec.key] ?? spec.key}_native_permission`;
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

/**
 * Resolve the vendor that spawned a native sub-agent child from its
 * `omnigent.wrapper` label (e.g. `"claude-code-native-ui-subagent"` →
 * the Claude Code spec). These children reuse the parent's agent row and
 * carry the VENDOR-side agent type as their `sub_agent_name` (Claude's
 * `subagent_type`, e.g. `"general-purpose"`), so the wrapper label is the
 * only signal for which product is running them.
 */
export function nativeCodingAgentForSubagentWrapper(
  wrapper: string | null | undefined,
): NativeCodingAgentSpec | undefined {
  return wrapper == null ? undefined : BY_SUBAGENT_WRAPPER.get(wrapper);
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

/**
 * Whether a harness is fully supported — the maintained, end-to-end tested
 * integrations that lead the picker. Everything else (including non-native
 * agents) belongs in the "More" group regardless of host readiness.
 */
export function isFullySupportedNativeCodingAgent(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
): boolean {
  return nativeCodingAgentForAvailableAgent(agent)?.fullySupported === true;
}

/**
 * Whether ``agent``'s harness is one of ``recentHarnesses``. Compares resolved
 * specs rather than raw strings so a stored reversed alias (``native-pi``) still
 * matches the canonical spelling (``pi-native``).
 */
export function isRecentHarness(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
  recentHarnesses: readonly string[],
): boolean {
  const spec = nativeCodingAgentForAvailableAgent(agent);
  if (spec === undefined) return false;
  return recentHarnesses.some((h) => nativeCodingAgentForHarness(h)?.key === spec.key);
}

export function isNativeWrapper(wrapper: string | null | undefined): boolean {
  return nativeCodingAgentForWrapper(wrapper) !== undefined;
}

/**
 * Whether a session runs a native terminal harness — by its `omnigent.wrapper`
 * label OR its resolved harness. Mirrors the server's
 * `_native_coding_agent_for_session`: a session is native-terminal if either
 * signal matches (a built-in wrapper agent sets the label; a custom agent bound
 * to a native harness has no label but still runs the native CLI). Native CLIs
 * bake the model at launch and can't per-turn route, so callers use this to hide
 * per-turn Smart Routing from these sessions.
 */
export function isNativeTerminalSession(
  session: { harness?: string | null; labels?: Record<string, string> } | null | undefined,
): boolean {
  if (session == null) return false;
  const wrapper = session.labels?.[WRAPPER_LABEL_KEY];
  if (isNativeWrapper(wrapper)) return true;
  return nativeCodingAgentForHarness(session.harness) !== undefined;
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
