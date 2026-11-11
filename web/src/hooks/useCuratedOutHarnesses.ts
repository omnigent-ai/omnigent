import { useMemo } from "react";
import { type AvailableAgent, useAgentCatalog } from "@/hooks/useAvailableAgents";
import { NATIVE_CODING_AGENTS, nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";

const NONE: ReadonlySet<string> = new Set();

/**
 * Native harnesses the deployment has curated out of Settings.
 *
 * A harness is out when the server hid its built-in agent
 * (`OMNIGENT_SEEDED_AGENTS`) and no agent the user can still pick runs on it;
 * one that some pickable agent needs stays, so the user can set it up.
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
  const inUse = new Set(agents.map((agent) => nativeCodingAgentForHarness(agent.harness)?.harness));
  return new Set(
    NATIVE_CODING_AGENTS.filter(
      (spec) => suppressedNames.has(spec.agentName) && !inUse.has(spec.harness),
    ).map((spec) => spec.harness),
  );
}

/**
 * Native harness slugs Settings leaves out, per {@link curatedOutHarnesses}.
 * Empty until the agent catalog loads, and when it fails, so a slow or older
 * server never hides a harness.
 *
 * @param enabled - Whether to fetch the agent catalog; defaults true.
 */
export function useCuratedOutHarnesses(enabled = true): ReadonlySet<string> {
  const { data: agents, suppressedNames } = useAgentCatalog(enabled);
  return useMemo(
    () => (agents === undefined ? NONE : curatedOutHarnesses(suppressedNames, agents)),
    [agents, suppressedNames],
  );
}
