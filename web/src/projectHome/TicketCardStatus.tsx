import { CirclePauseIcon, HourglassIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { getCurrentUserId } from "@/lib/identity";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import type { ProjectTicket } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";
import { useProjectRecord } from "./useProjectAgentSession";
import { useTicketActions } from "./useProjectTickets";
import { routerLabel, ticketStalled, waitTooLong, wakeRouterComment } from "./ticketStall";
import { needsWhom } from "./ticketWaitingOn";

/**
 * What a card's status icon says. `stalled` and `long_wait` are stuck: they count
 * toward the stalled chip and offer Wake the Lead; `waiting` is a quiet, expected wait.
 */
export type CardStatus =
  | {
      kind: "stalled" | "long_wait";
      label: string;
      detail: string;
      idleFor: string;
      waitingOn?: string;
    }
  | { kind: "waiting"; label: string; detail: string };

/** "Waiting until Nov 27, 10:00 AM" from a `waiting` label's "<ISO time> <note>". */
function untilLabel(value: string): { label: string; detail: string } {
  const [when = "", ...note] = value.trim().split(/\s+/);
  const at = Date.parse(when);
  const label = Number.isFinite(at) ? `Waiting until ${absoluteTime(at)}` : "Waiting";
  return { label, detail: note.join(" ") || "A planned wait; the work picks up then." };
}

/** What the wait is for, as the card names it, and why it usually takes a while. */
function waitText(
  ticket: ProjectTicket,
  viewer: string | null,
): { label: string; detail: string } | null {
  const wait = ticket.wait;
  if (!wait) return null;
  if (wait.kind === "person") {
    const whom = needsWhom(ticket.waiting_on, viewer);
    const named = whom?.kind === "other" && whom.title.startsWith("Waiting on");
    const label =
      whom?.kind === "you" ? "Waiting on you" : named ? whom.title : "Waiting on a person";
    return { label, detail: "It picks up once they answer on the ticket." };
  }
  if (wait.kind === "until") return untilLabel(wait.on[0] ?? "");
  if (wait.kind === "sign_off")
    return {
      label: `Waiting on ${wait.on.join(", ")}`,
      detail: "Reviewed and approved; it lands once a person adds the sign-off.",
    };
  return {
    label: "Waiting to land",
    detail: "Approved and verified; it lands in turn, often after other tickets.",
  };
}

export function cardStatus(
  ticket: ProjectTicket,
  nowMs: number,
  viewer: string | null,
): CardStatus | null {
  const wait = ticket.wait;
  const text = waitText(ticket, viewer);
  if (wait && text) {
    const idleFor = wait.since !== undefined ? relativeTime(wait.since * 1000, nowMs) : "";
    if (waitTooLong(ticket, nowMs))
      return {
        kind: "long_wait",
        label: `${text.label} · for ${idleFor}`,
        detail: `Nothing has moved for ${idleFor}, longer than this usually takes.`,
        idleFor,
        waitingOn: text.label,
      };
    // The card's "Needs …" chip already says it waits on a person.
    if (wait.kind === "person") return null;
    const detail =
      idleFor && idleFor !== "now" ? `${text.detail} Waiting ${idleFor} so far.` : text.detail;
    return { kind: "waiting", label: text.label, detail };
  }
  if (!ticket.stall || !ticketStalled(ticket, nowMs)) return null;
  const idleFor = relativeTime(ticket.stall.idle_since * 1000, nowMs);
  return {
    kind: "stalled",
    label: `Stalled · no one on this for ${idleFor}`,
    detail: `No agent is running and nothing has changed for ${idleFor}.`,
    idleFor,
  };
}

/** Holds the card footer's room for the status icon, which is drawn over it. */
export function TicketCardStatusSpace({ ticket, nowMs }: { ticket: ProjectTicket; nowMs: number }) {
  if (!cardStatus(ticket, nowMs, getCurrentUserId())) return null;
  // Width only: the icon is drawn over the row, so the row and card keep their height.
  return <span aria-hidden className="h-0 w-6 shrink-0" />;
}

const HOVER_CLOSE_MS = 150;

/**
 * A small icon in the card's footer row: amber when the work stalled or a wait ran
 * too long, muted for an expected wait. Hovering, or tapping on a phone, opens the
 * reason, and for stuck work Wake the Lead, which posts the viewer's comment
 * @mentioning the router.
 */
export function TicketCardStatus({ ticket, nowMs }: { ticket: ProjectTicket; nowMs: number }) {
  const status = cardStatus(ticket, nowMs, getCurrentUserId());
  if (!status) return null;
  return <StatusPopover ticket={ticket} status={status} />;
}

function StatusPopover({ ticket, status }: { ticket: ProjectTicket; status: CardStatus }) {
  const [open, setOpen] = useState(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => clearTimeout(closeTimer.current ?? undefined), []);
  const hover = {
    onMouseEnter: () => {
      clearTimeout(closeTimer.current ?? undefined);
      setOpen(true);
    },
    onMouseLeave: () => {
      closeTimer.current = setTimeout(() => setOpen(false), HOVER_CLOSE_MS);
    },
  };
  const stuck = status.kind !== "waiting";
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          {...hover}
          data-testid={stuck ? "ticket-stalled" : "ticket-waiting"}
          data-status={status.kind}
          aria-label={status.label}
          className={cn(
            // A 40px touch area around the 24px icon, without moving anything.
            "relative inline-flex size-6 shrink-0 items-center justify-center rounded-full outline-none after:absolute after:-inset-2 after:content-[''] focus-visible:ring-2 focus-visible:ring-ring pointer-fine:after:hidden",
            stuck
              ? "bg-amber-500/15 text-amber-700 hover:bg-amber-500/25 dark:text-amber-300"
              : "text-muted-foreground hover:bg-muted",
          )}
        >
          {status.kind === "stalled" ? (
            <CirclePauseIcon aria-hidden className="size-4" />
          ) : (
            <HourglassIcon aria-hidden className="size-3.5" />
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        {...hover}
        align="end"
        side="top"
        className="w-64"
        data-testid="ticket-status-popover"
        onOpenAutoFocus={(event) => event.preventDefault()}
      >
        {status.kind !== "waiting" ? (
          <StuckDetail ticket={ticket} status={status} />
        ) : (
          <div className="space-y-1 text-sm">
            <p className="font-medium">{status.label}</p>
            <p className="text-muted-foreground">{status.detail}</p>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}

function StuckDetail({
  ticket,
  status,
}: {
  ticket: ProjectTicket;
  status: Extract<CardStatus, { kind: "stalled" | "long_wait" }>;
}) {
  const { comment } = useTicketActions(ticket.project_id);
  // The board page already loads this record, so the popover adds no request.
  const router = useProjectRecord(ticket.project_id).data?.router?.name || "Lead";
  const who = routerLabel(router);
  return (
    <div className="space-y-2 text-sm">
      <div className="space-y-1">
        <p className="font-medium text-amber-800 dark:text-amber-300">{status.label}</p>
        <p className="text-muted-foreground">{status.detail}</p>
      </div>
      <Button
        size="sm"
        variant="outline"
        className="w-full"
        loading={comment.isPending}
        title={`Ask ${who} to find out why and get this moving`}
        onClick={() =>
          comment.mutate({
            id: ticket.id,
            body: {
              text: wakeRouterComment(router, status.idleFor, status.waitingOn),
              kind: "comment",
            },
          })
        }
      >
        Wake {who}
      </Button>
      {comment.isError && (
        <p role="alert" className="text-xs text-destructive">
          Couldn't wake {who}: {comment.error.message}
        </p>
      )}
    </div>
  );
}
