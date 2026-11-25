// After the viewer answers a ticket's question, what became of the answer: the answer
// itself, then the stored delivery state, and a Nudge once nobody has picked it up.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useNow } from "@/hooks/useNow";
import { nudgeTicket, type TicketAnswerReceipt as Receipt } from "@/lib/projectTicketsApi";
import { relativeTime } from "@/lib/relativeTime";
import { answerReceiptLine, canNudge } from "./answerReceipt";
import { type useProjectPersonaLookup, usePersonaForSession } from "./usePersonaSessions";

export function TicketAnswerReceipt({
  projectId,
  ticketId,
  receipt,
  personaLookup,
}: {
  projectId: string;
  ticketId: string;
  receipt: Receipt;
  personaLookup: ReturnType<typeof useProjectPersonaLookup>;
}) {
  const nowMs = useNow().getTime();
  const client = useQueryClient();
  const asker =
    usePersonaForSession(receipt.asker_session_id, personaLookup) ??
    (receipt.asker_session_id ? "the agent" : "the person who asked");
  const nudge = useMutation({
    mutationFn: () => nudgeTicket(projectId, ticketId),
    onSettled: () =>
      client.invalidateQueries({ queryKey: ["project-tickets", projectId, ticketId] }),
  });
  const answered = relativeTime(receipt.answered_at * 1000, nowMs);
  const failed = nudge.isError || (nudge.data && !nudge.data.delivered);
  return (
    <section
      role="region"
      aria-label="Your answer"
      data-testid="ticket-answer-receipt"
      className="space-y-1 rounded-lg border border-border/60 bg-muted/30 px-3 py-2.5 text-sm"
    >
      <p className="flex items-baseline gap-1.5">
        <CheckIcon aria-hidden className="size-3.5 shrink-0 self-center text-status-green" />
        <span className="font-medium">You answered</span>
        <span className="min-w-0 truncate text-muted-foreground">
          {receipt.text} · {answered === "now" ? "just now" : `${answered} ago`}
        </span>
      </p>
      <p className="text-muted-foreground" data-testid="ticket-answer-delivery">
        {answerReceiptLine(receipt, asker, nowMs)}
      </p>
      {canNudge(receipt, nowMs) && !nudge.data?.delivered && (
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Button
            type="button"
            size="xs"
            variant="outline"
            disabled={nudge.isPending}
            onClick={() => nudge.mutate()}
            componentId="ticket.answer.nudge"
          >
            {failed ? "Retry nudge" : "Nudge"}
          </Button>
          {failed && (
            <span role="alert" className="text-destructive">
              Couldn’t reach {asker}
              {nudge.data?.reason ? `: ${nudge.data.reason}` : ""}.
            </span>
          )}
        </div>
      )}
      {nudge.data?.delivered && (
        <p className="text-muted-foreground" data-testid="ticket-answer-nudged">
          Nudged {asker} just now.
        </p>
      )}
    </section>
  );
}
