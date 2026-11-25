import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ButtonGroup } from "@/components/ui/button-group";
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

/** "All tickets" / "Your tickets" toggle; "yours" means tickets you requested. */
export function TicketBoardRequesterFilter({
  mine,
  onChange,
  compact = false,
}: {
  mine: boolean;
  onChange: (mine: boolean) => void;
  /** A small segmented switch, for the end of the page's tab row. */
  compact?: boolean;
}) {
  const options = [
    { value: false, label: "All tickets" },
    { value: true, label: "Your tickets" },
  ];
  if (compact)
    // A pill switch, unlike the underlined tabs beside it: it filters the board, it isn't a page.
    return (
      <div
        role="group"
        aria-label="Filter tickets by requester"
        data-testid="ticket-requester-filter"
        className="inline-flex rounded-full border border-border bg-background p-0.5"
      >
        {options.map((option) => (
          <button
            key={option.label}
            type="button"
            aria-pressed={mine === option.value}
            title={option.value ? "Only tickets you requested" : undefined}
            onClick={() => onChange(option.value)}
            className={cn(
              "inline-flex h-7 items-center justify-center rounded-full px-3 text-[13px] leading-none font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              mine === option.value
                ? "bg-foreground text-background shadow-xs"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>
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
