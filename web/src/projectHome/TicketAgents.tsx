import { useEffect } from "react";
import { ChevronRightIcon, MessageSquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { ticketResponseSession, type ProjectTicket } from "@/lib/projectTicketsApi";
import { useProjectAgentSession } from "./useProjectAgentSession";
import { Link } from "@/lib/routing";
import { projectOrchestratorPath } from "./projectHomeNavigation";
import { TicketError } from "./TicketControls";
import { TicketSessionLink } from "./TicketSessionLink";
import { useTicketEvents, useTicketSessions } from "./useProjectTickets";

export function TicketAgents({
  projectId,
  projectName,
  ticket,
}: {
  projectId: string;
  projectName: string;
  ticket: ProjectTicket;
}) {
  const project = useProjectAgentSession(projectId);
  const query = useTicketSessions(projectId, ticket.id);
  const activity = useTicketEvents(projectId, ticket.id);
  const needsResponse = ticket.labels.some((label) => label.key === "needs-human");
  const { hasNextPage, isFetching, isError, fetchNextPage } = activity;
  useEffect(() => {
    // Reuse this open ticket's activity; partial history can point to an old request.
    if (needsResponse && hasNextPage && !isFetching && !isError) void fetchNextPage();
  }, [needsResponse, hasNextPage, isFetching, isError, fetchNextPage]);
  const responseSession = activity.isError
    ? null
    : ticketResponseSession(ticket, activity.data?.pages ?? []);
  const sessions = query.data?.pages.flatMap((page) => page.data) ?? [];
  const orchestratorId = project.sessionId;
  const orchestrator = sessions.find((session) => session.session_id === orchestratorId);
  return (
    <section aria-label="Agents" className="space-y-2">
      <h3 className="text-[length:calc(var(--text-ui)+2px)] font-bold">Agents</h3>
      <TicketError error={project.error ?? query.error} />
      <ul
        aria-label="Involved agents"
        className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border/60"
      >
        <li>
          {orchestratorId ? (
            <TicketSessionLink
              sessionId={orchestratorId}
              showStatus
              needsResponse={responseSession === orchestratorId}
              involvement={orchestrator?.involvement || "Coordinates work and handles questions"}
            />
          ) : (
            <Link
              to={projectOrchestratorPath(projectName)}
              aria-label="Open orchestrator"
              className="flex items-center gap-3 rounded-sm p-3 text-sm hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
            >
              <MessageSquareIcon aria-hidden className="size-4 text-muted-foreground" />
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
                showStatus
                needsResponse={responseSession === session.session_id}
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
