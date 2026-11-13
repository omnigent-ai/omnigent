import { useMemo } from "react";
import { type AvailableAgent, useAgentCatalog } from "@/hooks/useAvailableAgents";
import { NATIVE_CODING_AGENTS, nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";

const NONE: ReadonlySet<string> = new Set();

/**
 * Hide suppressed native harnesses unless a selectable agent uses them.
 *
 * @param suppressedNames - Built-in agent names the server hides, e.g. `{"codex-native-ui"}`.
 * @param agents - The agents the user can pick, e.g. from {@link useAgentCatalog}.
 * @returns Native harness slugs to leave out, e.g. `{"codex-native"}`.
 */
export function curatedOutHarnesses(
  suppressedNames: ReadonlySet<string>,
  agents: readonly Pick<AvailableAgent, "harness">[],
): ReadonlySet<string> {
  if (suppressedNames.size === 0) return NONE;
  // A harness the server couldn't read (e.g. a broken bundle) may be one it hides.
  if (agents.some((agent) => !agent.harness)) return NONE;
  const inUse = new Set(agents.map((agent) => nativeCodingAgentForHarness(agent.harness)?.harness));
  return new Set(
    NATIVE_CODING_AGENTS.filter(
      (spec) => suppressedNames.has(spec.agentName) && !inUse.has(spec.harness),
    ).map((spec) => spec.harness),
  );
}

/**
 * Native harness slugs Settings leaves out, per {@link curatedOutHarnesses}.
 * Empty unless every pickable agent is known: while either catalog request
 * loads or fails, when the user's own agents run past the read cap, and until
 * each agent only a recent session offers has its harness.
 *
 * @param enabled - Whether to fetch the agent catalog; defaults true.
 */
export function useCuratedOutHarnesses(enabled = true): ReadonlySet<string> {
  const { harnessAgents, suppressedNames, complete } = useAgentCatalog(enabled, {
    sessionAgents: true,
  });
  return useMemo(
    () => (!complete ? NONE : curatedOutHarnesses(suppressedNames, harnessAgents)),
    [harnessAgents, suppressedNames, complete],
  );
}
