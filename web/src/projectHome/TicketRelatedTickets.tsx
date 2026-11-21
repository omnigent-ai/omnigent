import { LinkIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Link } from "@/lib/routing";
import { ticketPanelSearch } from "./projectHomeNavigation";
import { TicketError, TicketStageIcon } from "./TicketControls";
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
      <h3 className="text-sm font-medium">Related tickets</h3>
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
