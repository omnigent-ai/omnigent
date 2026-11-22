import type { Agent } from "@/hooks/useAgents";

export const MAESTRO_DESCRIPTION =
  "Maestro leads your project like a conductor: organizing work into tickets and coordinating other agents to carry it out.";

/** Project identity follows the selected agent, not the main-session role. */
export function projectAgentDescription(
  agent: Pick<Agent, "name" | "description"> | undefined,
): string | null {
  return agent?.description?.trim() || null;
}
