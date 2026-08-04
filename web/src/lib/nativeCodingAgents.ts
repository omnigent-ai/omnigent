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
export type NativeCodingAgentCapability = "permissionMode" | "approvalMode" | "cursorMode";

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

/**
 * A native-agent row as published by the server on `GET /v1/harnesses`
 * (`native_agents[]`, PR 2.2). Carries identity + the fork-history axis; the
 * capabilities object is passed through opaquely (consumers that need a
 * specific axis read it by name). Presentation fields (iconKind / sortRank /
 * marketing displayName / picker capabilities) are NOT server-sourced — they
 * stay in the built-in literal above and default gracefully for unknown keys.
 */
export interface NativeAgentServerRow {
  key: string;
  agent_name: string;
  harness: string;
  wrapper_label: string;
  terminal_name: string;
  display_name: string;
  subagent_wrapper_label: string | null;
  fork_history: string | null;
  capabilities: Record<string, unknown> | null;
}

// Synchronous module cache of the server's native-agent rows, primed by the
// /v1/harnesses query (see primeNativeAgentCatalog). The many non-React
// consumers of this module read the merged view built-in-literal ∪ server-rows,
// so a community native harness resolves once the catalog has loaded, and the
// built-ins still work before the first fetch (they're in the literal).
let SERVER_ROWS: readonly NativeAgentServerRow[] = [];

/**
 * Turn a server row into the `NativeCodingAgentSpec` shape the module's lookups
 * return, borrowing presentation fields (iconKind / sortRank / displayName /
 * capabilities) from the same-`key` built-in literal when present, else safe
 * defaults: generic-bot icon (`iconKind` matching no branch), sort-last, the
 * server's `display_name`, and no picker capabilities. Plugin-provided icon
 * resources are a planned follow-up; today an unknown harness renders with the
 * generic glyph (same fallback qwen/hermes already use).
 */
function specFromServerRow(row: NativeAgentServerRow): NativeCodingAgentSpec {
  const builtin = BUILTIN_BY_KEY.get(row.key);
  return {
    key: (builtin?.key ?? row.key) as NativeCodingAgentIconKind,
    agentName: row.agent_name,
    harness: row.harness,
    wrapperLabel: row.wrapper_label,
    displayName: builtin?.displayName ?? row.display_name,
    iconKind: (builtin?.iconKind ?? row.key) as NativeCodingAgentIconKind,
    sortRank: builtin?.sortRank ?? Number.POSITIVE_INFINITY,
    capabilities: builtin?.capabilities,
  };
}

const BUILTIN_BY_KEY = new Map<string, NativeCodingAgentSpec>(
  NATIVE_CODING_AGENTS.map((agent) => [agent.key, agent]),
);

/**
 * Prime the module cache with the server's native-agent rows. Called by the
 * `/v1/harnesses` query (`agentLabels.ts`) whenever the catalog loads/refreshes.
 * A native harness the server reports but the built-in literal lacks (a
 * community plugin) becomes resolvable; built-ins are refreshed from the server
 * for identity/fork data while keeping their client-side presentation.
 */
export function primeNativeAgentCatalog(rows: readonly NativeAgentServerRow[]): void {
  SERVER_ROWS = rows;
  rebuildIndexes();
}

// Test-only: reset the cache to the built-in literal (no server rows).
export function resetNativeAgentCatalogForTests(): void {
  SERVER_ROWS = [];
  rebuildIndexes();
}

let BY_AGENT_NAME = new Map<string, NativeCodingAgentSpec>();
let BY_HARNESS = new Map<string, NativeCodingAgentSpec>();
let BY_WRAPPER = new Map<string, NativeCodingAgentSpec>();
// harness id (canonical) → fork_history axis value, server-sourced. Empty until
// the catalog primes; forkHarness.ts falls back to its built-in sets meanwhile.
let FORK_HISTORY_BY_HARNESS = new Map<string, string>();

function rebuildIndexes(): void {
  // Built-in literal first, then server rows override by the same key — so the
  // server is authoritative for identity/fork on built-ins, and community rows
  // are additive. Presentation still comes from the literal via specFromServerRow.
  const merged = new Map<string, NativeCodingAgentSpec>();
  for (const agent of NATIVE_CODING_AGENTS) merged.set(agent.key, agent);
  const forkByHarness = new Map<string, string>();
  for (const row of SERVER_ROWS) {
    merged.set(row.key, specFromServerRow(row));
    if (typeof row.fork_history === "string" && row.fork_history !== "none") {
      forkByHarness.set(row.harness, row.fork_history);
    }
  }
  const specs = [...merged.values()];
  BY_AGENT_NAME = new Map(specs.map((a) => [a.agentName, a]));
  BY_HARNESS = new Map(specs.map((a) => [a.harness, a]));
  BY_WRAPPER = new Map(specs.map((a) => [a.wrapperLabel, a]));
  FORK_HISTORY_BY_HARNESS = forkByHarness;
}

rebuildIndexes();

/**
 * Server-sourced fork-history axis for a canonical harness id, or `undefined`
 * before the catalog loads / for a harness the server doesn't classify.
 * `forkHarness.ts` consults this first and falls back to its built-in sets.
 */
export function serverForkHistoryForHarness(
  harness: string | null | undefined,
): string | undefined {
  return harness == null ? undefined : FORK_HISTORY_BY_HARNESS.get(harness);
}

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
