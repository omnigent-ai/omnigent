/**
 * Shared agent-picker grouping: the built-in vs custom split and the
 * preferred display order, used by both the new-session picker
 * (NewChatDialog) and the fork/switch picker (ForkSessionDialog) so the
 * two surfaces group and order agents identically.
 */
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { nativeAgentSortRank } from "@/lib/nativeCodingAgents";

// Built-in agents (by name slug) — the long-lived agents the server ships
// out of the box. Pickers group these first, then a divider, then custom
// (user-registered) agents. GET /v1/agents doesn't yet distinguish the
// two, so this is a frontend allowlist for now.
export const BUILTIN_AGENTS = new Set([
  "claude-native-ui", // Claude Code
  "codex-native-ui", // Codex
  "opencode-native-ui", // OpenCode
  "pi-native-ui", // Pi
  "cursor-native-ui", // Cursor
  "kiro-native-ui", // Kiro
  "antigravity-native-ui", // Antigravity
  "goose-native-ui", // Goose
  "qwen-native-ui", // Qwen Code
  "kimi-native-ui", // Kimi
  "polly",
  "debby",
]);

// Fallback only: builtin ACP CLI harness ids for servers whose harness catalog
// doesn't report `capabilities.integration_mode`. NOT the source of truth — a
// new builtin ACP row needs no entry here, because `useAvailableAgents` stamps
// `acpHarness` from the server catalog (see {@link isAcpHarnessAgent}).
const LEGACY_ACP_CLI_HARNESS_IDS = new Set<string>(["devin", "grok"]);

/**
 * Whether an agent is backed by the generic ACP harness — a user-configured
 * `acp:<slug>` agent (e.g. Kilocode) or a builtin ACP CLI harness (e.g. Devin,
 * Grok Build). These belong in the picker's "Harnesses" group with the native
 * CLIs: selecting one runs a harness, not a composed agent.
 *
 * Prefers the server's own answer (`acpHarness`, derived from the harness
 * catalog's `integration_mode`) so this frontend recognizes ACP harnesses it
 * has never heard of; the `acp:` prefix and the legacy id set are the
 * older-server fallback.
 *
 * @param agent - Agent to classify (only `harness` / `acpHarness` are read).
 */
export function isAcpHarnessAgent(
  agent: Pick<AvailableAgent, "harness" | "acpHarness"> | null | undefined,
): boolean {
  if (agent == null) return false;
  if (agent.acpHarness !== undefined) return agent.acpHarness;
  const harness = agent.harness;
  if (harness == null) return false;
  return harness.startsWith("acp:") || LEGACY_ACP_CLI_HARNESS_IDS.has(harness);
}

// Preferred display order for the built-in group. The server returns
// agents newest-registered first (agent_store.list sorts by created_at
// desc), so pin the order users expect; any agent not listed here falls
// after, in server order.
export const AGENT_DISPLAY_ORDER = [
  "Claude Code",
  "Codex",
  "OpenCode",
  "Cursor",
  "Pi",
  "Kiro",
  "Antigravity",
  "Qwen Code",
  "Kimi",
  "Polly",
  "Debby",
];

function displayRank(name: string): number {
  const i = AGENT_DISPLAY_ORDER.indexOf(name);
  return i === -1 ? AGENT_DISPLAY_ORDER.length : i;
}

/**
 * Sort agents into the picker's canonical order: native coding agents by
 * their sort rank first, then by {@link AGENT_DISPLAY_ORDER}. Stable, so
 * unranked names keep their incoming (server / scan) relative order.
 *
 * @param agents - Agents to sort (not mutated; a copy is returned).
 */
export function sortAgentsForDisplay<T extends AvailableAgent>(agents: readonly T[]): T[] {
  return [...agents].sort(
    (a, b) =>
      nativeAgentSortRank(a) - nativeAgentSortRank(b) ||
      displayRank(a.display_name) - displayRank(b.display_name),
  );
}

// Hidden from session-creation pickers. `nessie` is superseded by polly.
// `kimi` / `kimi-code` are the headless SDK harness (kept for sub-agent /
// `run --harness kimi` use) — pickers offer only the native TUI
// (`kimi-native-ui`).
export const NEW_SESSION_HIDDEN_AGENTS = new Set(["nessie", "kimi", "kimi-code"]);

/**
 * The pickable agent set for session-creation surfaces. The new-session
 * composer AND project settings must resolve the SAME set through this
 * helper — if they diverged, a project could pin a default agent the
 * composer refuses to show (and then silently substitutes another for).
 *
 * @param agents - Raw catalog + discovery output (e.g. useAvailableAgents).
 */
export function selectableSessionAgents<T extends AvailableAgent>(agents: readonly T[]): T[] {
  return sortAgentsForDisplay(agents.filter((a) => !NEW_SESSION_HIDDEN_AGENTS.has(a.name)));
}

/**
 * Sort then split agents into the built-in group and the custom group,
 * for rendering with a divider between. Built-ins are the
 * {@link BUILTIN_AGENTS} slugs; everything else is custom.
 *
 * @param agents - Agents to group (e.g. the picker's full candidate list).
 * @returns ``{ builtins, customs }``, each sorted via
 *   {@link sortAgentsForDisplay}.
 */
export function partitionAgentsByKind<T extends AvailableAgent>(
  agents: readonly T[],
): { builtins: T[]; customs: T[] } {
  const sorted = sortAgentsForDisplay(agents);
  // Prefer the server's ``builtin`` signal — GET /v1/agents now sets it
  // (session-scope-NULL row with a deterministic name-derived id). This groups
  // dynamically-seeded built-ins with the harnesses instead of under custom
  // agents; the {@link BUILTIN_AGENTS} allowlist is only a fallback for older
  // servers that don't send the field. Without this, seeded ACP agents (Devin,
  // grok, …) — whose names aren't in the static allowlist — fall to "custom".
  const isBuiltin = (a: T): boolean => a.builtin ?? BUILTIN_AGENTS.has(a.name);
  return {
    builtins: sorted.filter(isBuiltin),
    customs: sorted.filter((a) => !isBuiltin(a)),
  };
}
