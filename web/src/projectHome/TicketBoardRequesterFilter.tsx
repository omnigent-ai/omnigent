import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ButtonGroup } from "@/components/ui/button-group";
import { useViewerId } from "@/hooks/useViewerId";
import { userColor, userInitials } from "@/lib/userBadge";
import { cn } from "@/lib/utils";

function requesterFilterKey(projectId: string) {
  return `omnigent.ticketBoard.mine.${projectId}`;
}

function readMine(projectId: string | null): boolean {
  if (!projectId) return false;
  try {
    return window.localStorage.getItem(requesterFilterKey(projectId)) === "1";
  } catch {
    return false;
  }
}

// Same-tab copies of the filter (the page header and the board) follow one another.
const REQUESTER_FILTER_EVENT = "omnigent:ticket-board-mine";

/** Whether the board shows only the viewer's tickets; remembered per project. */
export function useBoardRequesterFilter(projectId: string | null) {
  const [mine, setMineState] = useState(() => readMine(projectId));
  useEffect(() => {
    setMineState(readMine(projectId));
    const sync = (event: Event) => {
      const detail = (event as CustomEvent<{ projectId: string; mine: boolean }>).detail;
      if (detail?.projectId === projectId) setMineState(detail.mine);
    };
    window.addEventListener(REQUESTER_FILTER_EVENT, sync);
    return () => window.removeEventListener(REQUESTER_FILTER_EVENT, sync);
  }, [projectId]);
  const setMine = (next: boolean) => {
    setMineState(next);
    if (!projectId) return;
    try {
      window.localStorage.setItem(requesterFilterKey(projectId), next ? "1" : "0");
    } catch {
      // Storage may be unavailable; the filter still applies for this view.
    }
    window.dispatchEvent(
      new CustomEvent(REQUESTER_FILTER_EVENT, { detail: { projectId, mine: next } }),
    );
  };
  return [mine, setMine] as const;
}

/** "All tickets" / "Your tickets" toggle, plus "Needs you"; "yours" means tickets you requested. */
/** The "Needs you (N)" chip: the tickets waiting on the viewer's answer. */
export interface NeedsYouChip {
  count: number;
  active: boolean;
  onSelect: () => void;
}

export function TicketBoardRequesterFilter({
  mine,
  onChange,
  needsYou,
  stalled,
  compact = false,
}: {
  mine: boolean;
  onChange: (mine: boolean) => void;
  /** Shown in the compact chips while anything waits on the viewer. */
  needsYou?: NeedsYouChip;
  /** "N stalled", shown in the compact chips while any loaded card is stalled. */
  stalled?: NeedsYouChip;
  /** A small segmented switch, for the end of the page's tab row. */
  compact?: boolean;
}) {
  const options = [
    { value: false, label: "All tickets" },
    { value: true, label: "Your tickets" },
  ];
  if (compact)
    return (
      <RequesterFilterChips mine={mine} onChange={onChange} needsYou={needsYou} stalled={stalled} />
    );
  return (
    <ButtonGroup aria-label="Filter tickets by requester" data-testid="ticket-requester-filter">
      {options.map((option) => (
        <Button
          key={option.label}
          type="button"
          size="sm"
          variant={mine === option.value ? "secondary" : "ghost"}
          aria-pressed={mine === option.value}
          title={option.value ? "Only tickets you requested" : undefined}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </Button>
      ))}
    </ButtonGroup>
  );
}

/** The view toolbar's filter chips; the selected chip takes the brand accent. */
function RequesterFilterChips({
  mine,
  onChange,
  needsYou,
  stalled,
}: {
  mine: boolean;
  onChange: (mine: boolean) => void;
  needsYou?: NeedsYouChip;
  stalled?: NeedsYouChip;
}) {
  const needsYouActive = needsYou?.active === true;
  // Either narrowing chip deselects All tickets and Yours.
  const narrowed = needsYouActive || stalled?.active === true;
  const viewerId = useViewerId();
  const chip = (selected: boolean) =>
    cn(
      "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full px-3 text-[13px] font-medium whitespace-nowrap ring-1 transition-colors ring-inset focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
      selected
        ? "bg-brand-accent/10 text-brand-accent ring-brand-accent/35"
        : "text-muted-foreground ring-border hover:bg-accent hover:text-foreground",
    );
  return (
    <div
      role="group"
      aria-label="Filter tickets by requester"
      data-testid="ticket-requester-filter"
      className="flex items-center gap-1.5"
    >
      <button
        type="button"
        aria-pressed={!mine && !narrowed}
        onClick={() => onChange(false)}
        className={chip(!mine && !narrowed)}
      >
        <span className="text-optical">All tickets</span>
      </button>
      <button
        type="button"
        aria-pressed={mine && !narrowed}
        aria-label="Your tickets"
        title="Only tickets you requested"
        onClick={() => onChange(true)}
        className={cn(chip(mine && !narrowed), viewerId && "pl-1")}
      >
        {viewerId && (
          <span
            aria-hidden
            className="inline-flex size-5 items-center justify-center rounded-full text-[9px] font-semibold text-white"
            style={{ backgroundColor: userColor(viewerId) }}
          >
            <span className="text-optical">{userInitials(viewerId)}</span>
          </span>
        )}
        <span className="text-optical">Yours</span>
      </button>
      {needsYou && needsYou.count > 0 && (
        <button
          type="button"
          aria-pressed={needsYouActive}
          title="Only tickets waiting on your answer"
          data-testid="ticket-needs-you-filter"
          onClick={needsYou.onSelect}
          className={chip(needsYouActive)}
        >
          <span className="text-optical">Needs you ({needsYou.count})</span>
        </button>
      )}
      {stalled && stalled.count > 0 && (
        <button
          type="button"
          aria-pressed={stalled.active}
          title="Only cards no one has worked on for a while"
          data-testid="ticket-stalled-filter"
          onClick={stalled.onSelect}
          className={cn(
            chip(stalled.active),
            !stalled.active &&
              "text-amber-800 ring-amber-500/40 hover:text-amber-800 dark:text-amber-300 dark:hover:text-amber-300",
          )}
        >
          <span className="text-optical">{stalled.count} stalled</span>
        </button>
      )}
    </div>
  );
}
