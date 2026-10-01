import type { AvailableAgent } from "@/hooks/useAvailableAgents";

/** A fresh available-agent record; identity and scenario-specific fields stay at the call site. */
export function testAgent(
  id: string,
  name: string,
  overrides: Partial<Omit<AvailableAgent, "id" | "name">> = {},
): AvailableAgent {
  return {
    id,
    name,
    display_name: name,
    description: null,
    harness: null,
    skills: [],
    ...overrides,
  };
}
