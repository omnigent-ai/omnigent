import type { ProjectIdentity } from "@/lib/projectsApi";
import { ASSIGNEE_LABEL_KEY } from "./TicketControls";

export interface TicketPersonaAgent {
  sessionId: string;
  name: string;
  involvement: string;
}

/**
 * Personas working a ticket without an explicit session link: the assignee,
 * and any whose current session wrote in the ticket's activity.
 */
export function ticketPersonaAgents({
  identities,
  labels,
  eventSessionIds,
  excludeSessionIds,
}: {
  identities: readonly ProjectIdentity[];
  labels: readonly { key: string; value?: string | null }[];
  eventSessionIds: ReadonlySet<string>;
  excludeSessionIds: ReadonlySet<string>;
}): TicketPersonaAgent[] {
  const assignees = new Set(
    labels
      .filter((label) => label.key === ASSIGNEE_LABEL_KEY && label.value)
      .map((label) => label.value!.trim().toLowerCase()),
  );
  const agents: TicketPersonaAgent[] = [];
  for (const identity of identities) {
    const sessionId = identity.active_session_id;
    if (!sessionId || excludeSessionIds.has(sessionId)) continue;
    const assigned = assignees.has(identity.name.trim().toLowerCase());
    if (!assigned && !eventSessionIds.has(sessionId)) continue;
    agents.push({
      sessionId,
      name: identity.name,
      involvement: assigned ? "Assigned to this ticket" : "Worked on this ticket",
    });
  }
  return agents;
}
