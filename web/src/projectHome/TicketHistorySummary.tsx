import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { SparklesIcon, XIcon } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  getTicketHistorySummary,
  summarizeTicketHistory,
  type TicketHistorySummary as Summary,
} from "@/lib/projectTicketsApi";
import { relativeTime } from "@/lib/relativeTime";
import { TicketError } from "./TicketControls";
import { TicketMarkdown } from "./ticketImages";

/** Tickets with more events than this offer a summary of their history. */
export const SUMMARY_MIN_EVENTS = 20;

function age(generatedAt: number, nowMs: number): string {
  const ago = relativeTime(generatedAt * 1000, nowMs);
  return ago === "now" ? "just now" : `${ago} ago`;
}

const summaryKey = (projectId: string, ticketId: string) =>
  ["project-ticket-summary", projectId, ticketId] as const;

/**
 * A "Summarize" control under a long ticket's activity. The summary opens only
 * when the viewer asks for it; nothing is posted to the ticket.
 */
export function TicketHistorySummary({
  projectId,
  ticketId,
  nowMs,
}: {
  projectId: string;
  ticketId: string;
  nowMs: number;
}) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const status = useQuery({
    queryKey: summaryKey(projectId, ticketId),
    queryFn: () => getTicketHistorySummary(projectId, ticketId),
    staleTime: 30_000,
  });
  const generate = useMutation({
    mutationFn: () => summarizeTicketHistory(projectId, ticketId),
    onSuccess: (data: Summary) => {
      client.setQueryData(summaryKey(projectId, ticketId), data);
      setOpen(true);
    },
  });
  if (!status.data?.available) return null;
  const summary = status.data.summary;
  const fresh = summary !== null && !status.data.stale;
  const showSummary = open && summary !== null;
  return (
    <div className="space-y-2" data-testid="ticket-history-summary">
      {!showSummary && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          loading={generate.isPending}
          onClick={() => (fresh ? setOpen(true) : generate.mutate())}
        >
          <SparklesIcon />
          {summary !== null && status.data.stale ? "Summarize again" : "Summarize"}
        </Button>
      )}
      <TicketError error={generate.error} />
      {showSummary && (
        <div
          role="region"
          aria-label="Ticket summary"
          className="space-y-2 rounded-md border border-border/70 bg-muted/40 p-3"
        >
          <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>
              Not posted to the ticket · {age(summary.generated_at, nowMs)}
              {status.data.stale && " · newer activity isn't included"}
            </span>
            <div className="flex items-center gap-1">
              {status.data.stale && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 text-xs"
                  loading={generate.isPending}
                  onClick={() => generate.mutate()}
                >
                  Update
                </Button>
              )}
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-6"
                aria-label="Hide summary"
                onClick={() => setOpen(false)}
              >
                <XIcon />
              </Button>
            </div>
          </div>
          <TicketMarkdown className="text-sm">{summary.text}</TicketMarkdown>
        </div>
      )}
    </div>
  );
}
