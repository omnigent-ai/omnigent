import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ButtonGroup } from "@/components/ui/button-group";

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

/** Whether the board shows only the viewer's tickets; remembered per project. */
export function useBoardRequesterFilter(projectId: string | null) {
  const [mine, setMineState] = useState(() => readMine(projectId));
  useEffect(() => setMineState(readMine(projectId)), [projectId]);
  const setMine = (next: boolean) => {
    setMineState(next);
    if (!projectId) return;
    try {
      window.localStorage.setItem(requesterFilterKey(projectId), next ? "1" : "0");
    } catch {
      // Storage may be unavailable; the filter still applies for this view.
    }
  };
  return [mine, setMine] as const;
}

/** "All tickets" / "Your tickets" toggle; "yours" means tickets you requested. */
export function TicketBoardRequesterFilter({
  mine,
  onChange,
}: {
  mine: boolean;
  onChange: (mine: boolean) => void;
}) {
  const options = [
    { value: false, label: "All tickets" },
    { value: true, label: "Your tickets" },
  ];
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
