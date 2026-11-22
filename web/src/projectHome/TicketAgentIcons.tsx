import { SessionAgentIcon } from "@/components/SessionAgentIcon";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { sessionAgentIdentity } from "@/lib/sessionAgentIdentity";

export function TicketAgentIcons({ ticket }: { ticket: ProjectTicket }) {
  const identities = new Map(
    (ticket.agents ?? []).flatMap((agent) => {
      const identity = sessionAgentIdentity({
        agentName: agent.agent_name,
        harness: agent.harness,
      });
      return identity ? [[identity.name.toLowerCase(), identity] as const] : [];
    }),
  );
  if (!identities.size) return null;
  return (
    <div
      role="group"
      aria-label="Ticket agents"
      className="relative z-10 -mt-3 mr-3 ml-3 flex flex-wrap justify-end gap-1"
    >
      {[...identities].map(([key, agent]) => (
        <SessionAgentIcon
          key={key}
          agent={agent}
          className="size-7 rounded-full border border-border/70 bg-card shadow-xs hover:bg-muted"
          iconClassName="size-[18px]"
        />
      ))}
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
