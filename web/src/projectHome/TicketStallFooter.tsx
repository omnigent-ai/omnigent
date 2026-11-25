import { CirclePauseIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { useTicketActions } from "./useProjectTickets";
import { ticketStalled, wakeLeadComment } from "./ticketStall";

/**
 * The amber band under a stalled card, with Wake the Lead. Waking posts the viewer's
 * comment @mentioning the Lead, so it takes the same permission as any comment, shows on
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
        title="Ask the Lead to find out why and get this moving"
        onClick={() =>
          comment.mutate({
            id: ticket.id,
            body: { text: wakeLeadComment(idleFor), kind: "comment" },
          })
        }
      >
        Wake the Lead
      </Button>
      {comment.isError && (
        <span role="alert" className="basis-full text-xs text-destructive">
          Couldn't wake the Lead: {comment.error.message}
        </span>
      )}
    </div>
  );
}
