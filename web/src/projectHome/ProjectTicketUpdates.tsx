import type { ReactNode } from "react";
import { ArrowRightIcon, CheckIcon } from "lucide-react";
import type { Bubble } from "@/lib/renderItems";
import { Link, useSearchParams } from "@/lib/routing";
import { ticketBatchSearch, ticketPanelSearch } from "./projectHomeNavigation";
import { ticketOperationBatches, type TicketOperationBatch } from "./ticketOperationBatches";
import { TicketStageBadge } from "./TicketStageAppearance";
import { useTicketSelection, useTicketStages } from "./useProjectTickets";

export function ticketResponseExtras(
  bubbles: Bubble[],
  projectId: string,
): ReadonlyMap<string, ReactNode> {
  return new Map(
    ticketOperationBatches(bubbles, projectId).map((batch) => [
      batch.bubbleId,
      <ProjectTicketUpdates key={batch.bubbleId} projectId={projectId} batch={batch} />,
    ]),
  );
}

export function ProjectTicketUpdates({
  projectId,
  batch,
}: {
  projectId: string;
  batch: TicketOperationBatch;
}) {
  const [search] = useSearchParams();
  const rows = useTicketSelection(projectId, batch.ticketIds.slice(0, 3));
  const configuration = useTicketStages(projectId);
  const total = batch.ticketIds.length;
  const title =
    batch.kind === "mixed"
      ? `${total} ticket updates`
      : `${total} ${total === 1 ? "ticket" : "tickets"} ${batch.kind}`;
  return (
    <section
      aria-label={title}
      className="my-3 w-full min-w-0 overflow-hidden rounded-xl border border-border/70 bg-card text-sm"
    >
      <h3 className="flex items-center gap-2 border-b border-border/50 px-3.5 py-3 font-medium">
        <CheckIcon aria-hidden className="size-4 text-muted-foreground" />
        {title}
      </h3>
      <ul className="divide-y divide-border/40">
        {rows.map((query, index) => {
          const ticket = query.isError ? undefined : query.data;
          const stage = configuration.data?.stages.find(
            (candidate) => candidate.id === ticket?.stage_id,
          );
          return (
            <li key={batch.ticketIds[index]}>
              {ticket ? (
                <Link
                  to={ticketPanelSearch(ticket.id, search)}
                  className="flex min-w-0 items-center gap-3 px-3.5 py-3 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                >
                  <span className="min-w-0 flex-1 truncate">{ticket.title}</span>
                  {stage && (
                    <span className="shrink-0 text-xs text-muted-foreground">
                      <TicketStageBadge stage={stage} />
                    </span>
                  )}
                </Link>
              ) : (
                <span className="block px-3.5 py-3 text-muted-foreground">
                  {query.isError ? "Ticket unavailable" : "Loading ticket…"}
                </span>
              )}
            </li>
          );
        })}
      </ul>
      <Link
        to={ticketBatchSearch(batch.ticketIds)}
        className="flex items-center justify-between border-t border-border/50 px-3.5 py-2.5 text-muted-foreground hover:bg-muted/40 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      >
        <span>{total > 3 ? `View all ${total}` : "View on board"}</span>
        <ArrowRightIcon aria-hidden className="size-3.5" />
      </Link>
    </section>
  );
}
