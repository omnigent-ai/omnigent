import { useQuery } from "@tanstack/react-query";
import { OctagonAlertIcon } from "lucide-react";
import { getPushPrefix, type PushPrefix } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";

// Shared by every card on the board: one read per project, refreshed with the board's pace.
const PUSH_PREFIX_STALE_MS = 30_000;

export function usePushPrefix(projectId: string) {
  return useQuery({
    queryKey: ["project-tickets", projectId, "push-prefix"],
    queryFn: () => getPushPrefix(projectId),
    staleTime: PUSH_PREFIX_STALE_MS,
    retry: false,
  }).data;
}

/** How many Verified tickets this ticket holds back from shipping, or 0. */
export function blockedCount(prefix: PushPrefix | null | undefined, ticketId: string): number {
  return prefix?.stop?.ticket_id === ticketId ? prefix.blocked_verified_ticket_ids.length : 0;
}

/** The panel's next step. The signal only shows on the stop ticket itself, so it speaks to that. */
export const BLOCKING_NEXT_STEP =
  "This ticket has landed but isn't verified yet. Verify it or revert it to let the others ship.";
/** For a Verified ticket whose verified commit hasn't fully landed. */
export const BLOCKING_PARTIAL_STEP =
  "Only part of this ticket has landed. Land the rest, or revert it, to let the others ship.";

/** What to do about the stop ticket, from what the report says about it. */
export function blockingNextStep(prefix: PushPrefix | null | undefined, ticketId: string): string {
  const row = prefix?.tickets?.find((ticket) => ticket.ticket_id === ticketId);
  return row?.verified ? BLOCKING_PARTIAL_STEP : BLOCKING_NEXT_STEP;
}

export function blockingLabel(count: number): string {
  return `Blocking ${count} Verified ${count === 1 ? "ticket" : "tickets"} from shipping`;
}

/** On the first landed-but-unverified ticket: the Verified work waiting behind it. */
export function TicketShipBlocker({
  projectId,
  ticketId,
  variant,
}: {
  projectId: string;
  ticketId: string;
  variant: "card" | "panel";
}) {
  const prefix = usePushPrefix(projectId);
  const count = blockedCount(prefix, ticketId);
  if (count === 0) return null;
  return (
    <div
      role="status"
      data-testid="ticket-ship-blocker"
      className={cn(
        "flex min-w-0 items-start gap-1.5 rounded-md border border-warning/50 bg-warning/10 text-warning",
        variant === "card" ? "px-2 py-1 text-xs font-medium" : "px-3 py-2 text-sm",
      )}
    >
      <OctagonAlertIcon
        aria-hidden
        className={cn("shrink-0", variant === "card" ? "mt-px size-3.5" : "mt-0.5 size-4")}
      />
      <span className="min-w-0">
        <span className="font-medium">{blockingLabel(count)}</span>
        {variant === "panel" && (
          <span className="block text-foreground/80">{blockingNextStep(prefix, ticketId)}</span>
        )}
      </span>
    </div>
  );
}
