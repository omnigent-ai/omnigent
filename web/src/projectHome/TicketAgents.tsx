import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BotIcon, ChevronRightIcon, PencilIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import type { ProjectTicket, TicketSession } from "@/lib/projectTicketsApi";
import { getProject } from "@/lib/projectsApi";
import { Link } from "@/lib/routing";
import { ticketSessionSearch } from "./projectHomeNavigation";
import { TicketError } from "./TicketControls";
import { TicketSessionLink } from "./TicketSessionLink";
import {
  useTicketActions,
  useTicketInvolvementEnabled,
  useTicketSessions,
} from "./useProjectTickets";

export function TicketAgents({ projectId, ticket }: { projectId: string; ticket: ProjectTicket }) {
  const project = useQuery({
    queryKey: ["project-record", projectId],
    queryFn: () => getProject(projectId),
    refetchInterval: 15_000,
  });
  const query = useTicketSessions(projectId, ticket.id);
  const canEdit = useTicketInvolvementEnabled();
  const sessions = query.data?.pages.flatMap((page) => page.data) ?? [];
  const orchestratorId = project.data?.orchestrator_session_id;
  const orchestrator = sessions.find((session) => session.session_id === orchestratorId);
  const responseTag = ticket.tags.find((tag) => tag.key === "needs-human");
  const responseSessionId = responseTag ? ticket.lead_session_id : null;
  return (
    <section aria-label="Agents" className="space-y-2">
      <h3 className="text-sm font-medium">Agents</h3>
      {!responseTag && (
        <p className="text-sm text-muted-foreground">
          Open a chat to discuss this ticket or answer an agent’s prompt.
        </p>
      )}
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
            <li key={session.session_id} className="flex min-w-0 items-start pr-1">
              <TicketSessionLink
                sessionId={session.session_id}
                ticketId={ticket.id}
                showStatus
                involvement={session.involvement}
                responseRequested={session.session_id === responseSessionId}
              />
              {canEdit && (
                <InvolvementEditor projectId={projectId} ticketId={ticket.id} session={session} />
              )}
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

function InvolvementEditor({
  projectId,
  ticketId,
  session,
}: {
  projectId: string;
  ticketId: string;
  session: TicketSession;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const { involvement } = useTicketActions(projectId);
  return (
    <Popover
      open={open}
      onOpenChange={(value) => {
        setOpen(value);
        if (value) {
          setText(session.involvement ?? "");
          involvement.reset();
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Edit agent involvement"
          className="mt-2 text-muted-foreground"
        >
          <PencilIcon aria-hidden />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 max-w-[calc(100vw-2rem)]">
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (involvement.isPending) return;
            involvement.mutate(
              { id: ticketId, sessionId: session.session_id, text: text.trim() },
              { onSuccess: () => setOpen(false) },
            );
          }}
        >
          <label className="grid gap-2 text-sm font-medium">
            Involvement
            <Input
              value={text}
              maxLength={300}
              disabled={involvement.isPending}
              placeholder="e.g. Investigating the scheduling approach"
              onChange={(event) => setText(event.target.value)}
            />
          </label>
          <TicketError error={involvement.error} />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" type="button" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" type="submit" loading={involvement.isPending}>
              Save
            </Button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
  );
}
