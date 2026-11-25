import { CornerDownRightIcon, LinkIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Link } from "@/lib/routing";
import { ticketPanelSearch } from "./projectHomeNavigation";
import { TicketError, TicketStageIcon } from "./TicketControls";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { useRelatedTickets, useTicketStages } from "./useProjectTickets";

export function TicketRelatedTickets({
  projectId,
  ticketId,
}: {
  projectId: string;
  ticketId: string;
}) {
  const configuration = useTicketStages(projectId);
  const stageFor = (id: string) => configuration.data?.stages.find((stage) => stage.id === id);
  const related = useRelatedTickets(projectId, ticketId);
  const tickets = related.data?.pages.flatMap((page) => page.data) ?? [];
  if (related.isSuccess && tickets.length === 0 && !related.hasNextPage) return null;
  return (
    <section aria-label="Related tickets" className="space-y-2">
      <h3 className="text-[length:calc(var(--text-ui)+2px)] font-bold">Related tickets</h3>
      <TicketError error={related.error} />
      {related.isPending && <Spinner aria-label="Loading related tickets" />}
      {tickets.length > 0 && (
        <ul className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border/60">
          {tickets.map((ticket) => (
            <li key={ticket.id}>
              <Link
                to={ticketPanelSearch(ticket.id)}
                aria-label={`${ticket.title} — ${stageFor(ticket.stage_id)?.name ?? ticket.stage_id}`}
                className="flex min-w-0 flex-1 items-center gap-3 rounded-sm p-3 transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
              >
                <LinkIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 text-sm font-medium wrap-anywhere">
                  {ticket.title}
                </span>
                <span className="flex shrink-0 items-center gap-1.5 text-sm text-muted-foreground">
                  <TicketStageIcon stage={stageFor(ticket.stage_id)} />
                  {stageFor(ticket.stage_id)?.name ?? ticket.stage_id}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
      {related.hasNextPage && (
        <Button
          variant="ghost"
          size="sm"
          loading={related.isFetchingNextPage}
          onClick={() => void related.fetchNextPage()}
        >
          More related tickets
        </Button>
      )}
    </section>
  );
}

/**
 * On a completed ticket, the linked tickets filed after its final update: the follow-ups
 * a later comment led to, shown at the top so the hand-off isn't missed.
 */
export function TicketFollowUps({
  projectId,
  ticket,
}: {
  projectId: string;
  ticket: Pick<ProjectTicket, "id" | "stage_id" | "final_update">;
}) {
  const configuration = useTicketStages(projectId);
  const stages = configuration.data?.stages ?? [];
  const related = useRelatedTickets(projectId, ticket.id);
  const completedAt = ticket.final_update?.created_at;
  if (completedAt === undefined || stages.at(-1)?.id !== ticket.stage_id) return null;
  const followUps = (related.data?.pages.flatMap((page) => page.data) ?? []).filter(
    (other) => other.created_at > completedAt,
  );
  if (followUps.length === 0) return null;
  const stageFor = (id: string) => stages.find((stage) => stage.id === id);
  return (
    <section aria-label="Follow-ups" className="space-y-1">
      {followUps.map((followUp) => (
        <Link
          key={followUp.id}
          to={ticketPanelSearch(followUp.id)}
          className="flex min-w-0 items-center gap-2 rounded-md border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-sm hover:bg-sky-500/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <CornerDownRightIcon aria-hidden className="size-4 shrink-0 text-sky-600" />
          <span className="shrink-0 font-medium">Follow-up:</span>
          <span className="min-w-0 flex-1 truncate">{followUp.title}</span>
          <span className="flex shrink-0 items-center gap-1.5 text-muted-foreground">
            <TicketStageIcon stage={stageFor(followUp.stage_id)} />
            {stageFor(followUp.stage_id)?.name ?? followUp.stage_id}
          </span>
        </Link>
      ))}
    </section>
  );
}
