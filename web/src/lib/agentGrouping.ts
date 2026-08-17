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

// Builtin ACP CLI harness ids (mirrors ACP_CLI_HARNESSES on the server). Like
// the native harnesses, these are harness-backed picks — not composed agents —
// so the picker groups them under "Harnesses ▸ More", beside OpenCode/Cursor.
// User-configured ACP agents carry an `acp:<slug>` harness; a builtin ACP CLI
// harness carries a bare id.
export const ACP_CLI_HARNESS_IDS = new Set<string>(["grok"]);

/**
 * Whether an agent is backed by the generic ACP harness — a configured
 * `acp:<slug>` agent (e.g. Devin, Kilocode) or a builtin ACP CLI harness
 * (e.g. Grok). These belong in the picker's "Harnesses" group with the native
 * CLIs: selecting one runs a harness, not a composed agent.
 *
 * @param agent - Agent to classify (only its `harness` is read).
 */
export function isAcpHarnessAgent(
  agent: Pick<AvailableAgent, "harness"> | null | undefined,
): boolean {
  const harness = agent?.harness;
  if (harness == null) return false;
  return harness.startsWith("acp:") || ACP_CLI_HARNESS_IDS.has(harness);
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
