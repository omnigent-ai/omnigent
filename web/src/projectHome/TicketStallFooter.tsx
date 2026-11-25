import { CirclePauseIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { useProjectRecord } from "./useProjectAgentSession";
import { useTicketActions } from "./useProjectTickets";
import { routerLabel, ticketStalled, wakeRouterComment } from "./ticketStall";

/**
 * The amber band under a stalled card, with Wake the Lead. Waking posts the viewer's
 * comment @mentioning the project's router (the Lead unless the owner chose another), so it takes the same permission as any comment, shows on
 * the ticket, and, as new activity, clears the band until the card idles again.
 */
export function TicketStallFooter({ ticket, nowMs }: { ticket: ProjectTicket; nowMs: number }) {
  if (!ticket.stall || !ticketStalled(ticket, nowMs)) return null;
  return <StalledBand ticket={ticket} since={ticket.stall.idle_since * 1000} nowMs={nowMs} />;
}

function StalledBand({
  ticket,
  since,
  nowMs,
}: {
  ticket: ProjectTicket;
  since: number;
  nowMs: number;
}) {
  const { comment } = useTicketActions(ticket.project_id);
  // The board page already loads this record, so the band adds no request.
  const router = useProjectRecord(ticket.project_id).data?.router?.name || "Lead";
  const who = routerLabel(router);
  const idleFor = relativeTime(since, nowMs);
  return (
    <div
      data-testid="ticket-stalled"
      className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-b-xl border border-t-0 border-amber-500/40 bg-amber-500/10 px-3.5 py-1.5 text-sm text-amber-800 dark:text-amber-300"
    >
      <CirclePauseIcon aria-hidden className="size-3.5 shrink-0" />
      <span className="min-w-0 flex-1 basis-44" title={`No activity since ${absoluteTime(since)}`}>
        Stalled · no one on this for {idleFor}
      </span>
      <Button
        variant="ghost"
        size="xs"
        className="-mr-1.5 ml-auto font-semibold text-inherit underline-offset-2 hover:bg-amber-500/15 hover:text-inherit hover:underline"
        loading={comment.isPending}
        title={`Ask ${who} to find out why and get this moving`}
        onClick={() =>
          comment.mutate({
            id: ticket.id,
            body: { text: wakeRouterComment(router, idleFor), kind: "comment" },
          })
        }
      >
        Wake {who}
      </Button>
      {comment.isError && (
        <span role="alert" className="basis-full text-xs text-destructive">
          Couldn't wake {who}: {comment.error.message}
        </span>
      )}
    </div>
  );
}
