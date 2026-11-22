import { ChevronRightIcon, MessageSquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { useProjectAgentSession } from "./useProjectAgentSession";
import { Link, useSearchParams } from "@/lib/routing";
import { ticketSessionSearch } from "./projectHomeNavigation";
import { TicketError } from "./TicketControls";
import { TicketSessionLink } from "./TicketSessionLink";
import { useTicketSessions } from "./useProjectTickets";

export function TicketAgents({ projectId, ticket }: { projectId: string; ticket: ProjectTicket }) {
  const [search] = useSearchParams();
  const project = useProjectAgentSession(projectId);
  const query = useTicketSessions(projectId, ticket.id);
  const sessions = query.data?.pages.flatMap((page) => page.data) ?? [];
  const orchestratorId = project.sessionId;
  const orchestrator = sessions.find((session) => session.session_id === orchestratorId);
  return (
    <section aria-label="Agents" className="space-y-2">
      <h3 className="text-sm font-medium">Agents</h3>
      <TicketError error={project.error ?? query.error} />
      <ul
        aria-label="Involved agents"
        className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border/60"
      >
        <li>
          {orchestratorId ? (
            <TicketSessionLink
              sessionId={orchestratorId}
              ticketId={ticket.id}
              to={ticketSessionSearch(ticket.id, undefined, search)}
              showStatus
              involvement={orchestrator?.involvement || "Coordinates work and handles questions"}
            />
          ) : (
            <Link
              to={ticketSessionSearch(ticket.id, undefined, search)}
              aria-label="Open project chat"
              className="flex items-center gap-3 rounded-sm p-3 text-sm hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
            >
              <MessageSquareIcon aria-hidden className="size-4 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block font-medium">Chat</span>
                <span className="block text-muted-foreground">
                  Coordinates work and handles questions
                </span>
                <span className="mt-2 flex items-center gap-1 font-medium">
                  Open chat
                  <ChevronRightIcon aria-hidden className="size-3.5" />
                </span>
              </span>
            </Link>
          )}
        </li>
        {sessions
          .filter((session) => session.session_id !== orchestratorId)
          .map((session) => (
            <li key={session.session_id}>
              <TicketSessionLink
                sessionId={session.session_id}
                ticketId={ticket.id}
                showStatus
                involvement={session.involvement}
              />
            </li>
          ))}
      </ul>
      {query.isPending && <Spinner aria-label="Loading agents" />}
      {query.hasNextPage && (
        <Button
          variant="ghost"
          size="sm"
          loading={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          More agents
        </Button>
      )}
    </section>
  );
}
