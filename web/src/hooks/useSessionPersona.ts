import { useSessionLabel, type SessionLabel } from "@/hooks/useSessionLabel";
import { sidebarPersonaName, useOrchestratorSessionIds } from "@/projectHome/usePersonaSessions";

/** A session's label plus the team member it works as: "Lead", a persona's name, or null. */
export interface SessionPersona {
  label: SessionLabel | null;
  persona: string | null;
}

/**
 * Which project team member session ``sessionId`` is. The project's main
 * session is the Lead; an identity session is titled with its persona's name.
 */
export function useSessionPersona(
  sessionId: string | null,
  currentSessionId: string | null,
): SessionPersona {
  const label = useSessionLabel(sessionId, currentSessionId);
  const leadSessionIds = useOrchestratorSessionIds();
  if (!sessionId) return { label, persona: null };
  const persona = sidebarPersonaName(
    { id: sessionId, title: label?.title, labels: label?.labels },
    leadSessionIds,
  );
  return { label, persona };
}
