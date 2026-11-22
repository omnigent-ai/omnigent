import { useQuery } from "@tanstack/react-query";
import { BotIcon, ChevronRightIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { getProject } from "@/lib/projectsApi";
import { Link } from "@/lib/routing";
import { ticketSessionSearch } from "./projectHomeNavigation";
import { TicketError } from "./TicketControls";
import { TicketSessionLink } from "./TicketSessionLink";
import { useTicketSessions } from "./useProjectTickets";

export function TicketAgents({ projectId, ticket }: { projectId: string; ticket: ProjectTicket }) {
  const project = useQuery({
    queryKey: ["project-record", projectId],
    queryFn: () => getProject(projectId),
    refetchInterval: 15_000,
  });
  const query = useTicketSessions(projectId, ticket.id);
  const sessions = query.data?.pages.flatMap((page) => page.data) ?? [];
  const orchestratorId = project.data?.orchestrator_session_id;
  const orchestrator = sessions.find((session) => session.session_id === orchestratorId);
  const responseLabel = ticket.labels.find((label) => label.key === "needs-human");
  const responseSessionId = responseLabel ? ticket.lead_session_id : null;
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
              showStatus
              involvement={orchestrator?.involvement || "Coordinates work and handles questions"}
              responseRequested={orchestratorId === responseSessionId}
            />
          ) : (
            <Link
              to={ticketSessionSearch(ticket.id)}
              aria-label="Open orchestrator chat"
              className="flex items-center gap-3 rounded-sm p-3 text-sm hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
            >
              <BotIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block font-medium">Orchestrator</span>
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
                responseRequested={session.session_id === responseSessionId}
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
