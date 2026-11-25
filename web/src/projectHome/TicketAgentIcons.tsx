import { PersonaAvatar } from "@/components/PersonaAvatar";
import { SessionAgentIcon } from "@/components/SessionAgentIcon";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { sessionAgentIdentity, type SessionAgentIdentity } from "@/lib/sessionAgentIdentity";
import { cn } from "@/lib/utils";

const ORB_CLASS = "size-6 rounded-full border border-border/70 bg-card shadow-xs";
/** One orb's width plus the gap between orbs (size-6, gap-1), in px. */
const ORB_STEP_PX = 28;

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

function orbCount(ticket: ProjectTicket) {
  return ticketAgentOrbs(ticket.agents ?? []).size + (ticket.agents_has_more ? 1 : 0);
}

/** Holds the card footer's space for the agent icons, which are drawn over it. */
export function TicketAgentIconsSpace({ ticket }: { ticket: ProjectTicket }) {
  const count = orbCount(ticket);
  if (!count) return null;
  return <span aria-hidden className="h-6 shrink-0" style={{ width: count * ORB_STEP_PX - 4 }} />;
}

export function TicketAgentIcons({
  ticket,
  className,
}: {
  ticket: ProjectTicket;
  className?: string;
}) {
  const orbs = ticketAgentOrbs(ticket.agents ?? []);
  if (!orbs.size) return null;
  return (
    <div
      role="group"
      aria-label="Ticket agents"
      className={cn("z-10 flex gap-1", className)}
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
            <PersonaAvatar name={orb.persona} size="sm" />
          </span>
        ) : (
          <SessionAgentIcon
            key={key}
            agent={orb.agent}
            className={`${ORB_CLASS} hover:bg-muted`}
            iconClassName="size-4"
          />
        ),
      )}
      {ticket.agents_has_more && (
        <span
          aria-label="More agents"
          title="More agents"
          className="inline-flex size-6 items-center justify-center rounded-full border border-border/70 bg-card text-xs text-muted-foreground shadow-xs"
        >
          +
        </span>
      )}
    </div>
  );
}
