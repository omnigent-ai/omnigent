import { useState } from "react";
import { LinkIcon, PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import { TICKET_STATUS_LABELS } from "@/lib/projectTicketsApi";
import { Link } from "@/lib/routing";
import { ticketPanelSearch } from "./projectHomeNavigation";
import { TicketError, TicketStatusIcon } from "./TicketControls";
import { useRelatedTickets, useTicketActions, useTickets } from "./useProjectTickets";

export function TicketRelatedTickets({
  projectId,
  ticketId,
}: {
  projectId: string;
  ticketId: string;
}) {
  const [open, setOpen] = useState(false);
  const related = useRelatedTickets(projectId, ticketId);
  const candidates = useTickets(open ? projectId : null);
  const { link } = useTicketActions(projectId);
  const tickets = related.data?.pages.flatMap((page) => page.data) ?? [];
  const linkedIds = new Set([ticketId, ...tickets.map((ticket) => ticket.id)]);
  const available =
    candidates.data?.pages
      .flatMap((page) => page.data)
      .filter((ticket) => !linkedIds.has(ticket.id)) ?? [];
  return (
    <section aria-label="Related tickets" className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-medium">Related tickets</h3>
        <Popover
          open={open}
          onOpenChange={(value) => {
            setOpen(value);
            link.reset();
          }}
        >
          <PopoverTrigger asChild>
            <Button variant="ghost" size="sm" className="text-muted-foreground">
              <PlusIcon aria-hidden /> Link ticket
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-80 max-w-[calc(100vw-2rem)] p-2">
            <h4 className="px-2 py-1 text-sm font-medium">Link related work</h4>
            <div className="max-h-64 overflow-y-auto">
              {candidates.isPending && (
                <Spinner aria-label="Loading tickets to link" className="m-3" />
              )}
              {available.map((ticket) => (
                <Button
                  key={ticket.id}
                  variant="ghost"
                  disabled={link.isPending}
                  className="h-auto w-full justify-start py-2 text-left whitespace-normal"
                  onClick={() =>
                    link.mutate(
                      { id: ticketId, relatedId: ticket.id, action: "attach" },
                      { onSuccess: () => setOpen(false) },
                    )
                  }
                >
                  <TicketStatusIcon status={ticket.status} />
                  <span className="min-w-0 wrap-anywhere">{ticket.title}</span>
                </Button>
              ))}
              {candidates.isSuccess && available.length === 0 && (
                <p className="p-2 text-sm text-muted-foreground">
                  {candidates.hasNextPage
                    ? "Load more tickets to find related work."
                    : "No other tickets to link."}
                </p>
              )}
              {candidates.hasNextPage && (
                <Button
                  variant="ghost"
                  size="sm"
                  loading={candidates.isFetchingNextPage}
                  onClick={() => void candidates.fetchNextPage()}
                >
                  More tickets
                </Button>
              )}
            </div>
            <TicketError error={candidates.error ?? link.error} />
          </PopoverContent>
        </Popover>
      </div>
      <TicketError error={related.error ?? (!open ? link.error : null)} />
      {related.isPending && <Spinner aria-label="Loading related tickets" />}
      {tickets.length > 0 && (
        <ul className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border/60">
          {tickets.map((ticket) => (
            <li key={ticket.id} className="flex min-w-0 items-center gap-1 pr-1">
              <Link
                to={ticketPanelSearch(ticket.id)}
                aria-label={`${ticket.title} — ${TICKET_STATUS_LABELS[ticket.status]}`}
                className="flex min-w-0 flex-1 items-center gap-3 rounded-sm p-3 transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
              >
                <LinkIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 text-sm font-medium wrap-anywhere">
                  {ticket.title}
                </span>
                <span className="flex shrink-0 items-center gap-1.5 text-sm text-muted-foreground">
                  <TicketStatusIcon status={ticket.status} />
                  {TICKET_STATUS_LABELS[ticket.status]}
                </span>
              </Link>
              <Button
                variant="ghost"
                size="icon-sm"
                disabled={link.isPending}
                aria-label={`Unlink ${ticket.title}`}
                onClick={() =>
                  link.mutate({ id: ticketId, relatedId: ticket.id, action: "remove" })
                }
              >
                <XIcon aria-hidden />
              </Button>
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
