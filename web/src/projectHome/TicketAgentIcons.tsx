import { PersonaAvatar } from "@/components/PersonaAvatar";
import { SessionAgentIcon } from "@/components/SessionAgentIcon";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { sessionAgentIdentity, type SessionAgentIdentity } from "@/lib/sessionAgentIdentity";

const ORB_CLASS = "size-7 rounded-full border border-border/70 bg-card shadow-xs";

type TicketAgentOrb = { persona: string } | { agent: SessionAgentIdentity };

/** One orb per persona, then one per remaining agent kind. */
export function ticketAgentOrbs(agents: NonNullable<ProjectTicket["agents"]>) {
  const orbs = new Map<string, TicketAgentOrb>();
  for (const agent of agents) {
    if (agent.persona) {
      orbs.set(`persona:${agent.persona}`, { persona: agent.persona });
      continue;
    }
    const identity = sessionAgentIdentity({ agentName: agent.agent_name, harness: agent.harness });
    if (identity) orbs.set(`agent:${identity.name.toLowerCase()}`, { agent: identity });
  }
  return orbs;
}

export function TicketAgentIcons({ ticket }: { ticket: ProjectTicket }) {
  const orbs = ticketAgentOrbs(ticket.agents ?? []);
  if (!orbs.size) return null;
  return (
    <div
      role="group"
      aria-label="Ticket agents"
      className="relative z-10 -mt-3 mr-3 ml-3 flex flex-wrap justify-end gap-1"
    >
      {[...orbs].map(([key, orb]) =>
        "persona" in orb ? (
          <span
            key={key}
            role="img"
            aria-label={orb.persona}
            title={orb.persona}
            className={`inline-flex items-center justify-center ${ORB_CLASS}`}
          >
            <PersonaAvatar name={orb.persona} size="md" />
          </span>
        ) : (
          <SessionAgentIcon
            key={key}
            agent={orb.agent}
            className={`${ORB_CLASS} hover:bg-muted`}
            iconClassName="size-[18px]"
          />
        ),
      )}
      {ticket.agents_has_more && (
        <span
          aria-label="More agents"
          title="More agents"
          className="inline-flex size-7 items-center justify-center rounded-full border border-border/70 bg-card text-xs text-muted-foreground shadow-xs"
        >
          +
        </span>
      )}
    </div>
  );
}
