// A one-time note on the Summary when a project finishes its 10th, 100th or 250th
// ticket. The count comes from the completed lane's query, which the board already
// runs, and the dismissal lives in this browser: no extra server work.

import { XIcon } from "lucide-react";
import { useState } from "react";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { Button } from "@/components/ui/button";
import { usePlayfulTouches } from "@/lib/playfulTouchesPreferences";
import { useTickets, useTicketStages } from "./useProjectTickets";

export const MILESTONES = [10, 100, 250] as const;

const seenKey = (projectId: string) => `omnigent:milestone-seen:${projectId}`;

/** The highest milestone reached that hasn't been dismissed yet, or null. */
export function milestoneToShow(finished: number | null, seen: number): number | null {
  if (finished === null) return null;
  const reached = MILESTONES.filter((milestone) => finished >= milestone).at(-1);
  return reached !== undefined && reached > seen ? reached : null;
}

function readSeen(projectId: string): number {
  try {
    return Number(window.localStorage.getItem(seenKey(projectId))) || 0;
  } catch {
    return 0;
  }
}

export function ProjectMilestoneNote({ projectId }: { projectId: string }) {
  const playful = usePlayfulTouches();
  const stages = useTicketStages(projectId);
  const completed = stages.data?.stages.find((stage) => stage.is_complete);
  const lane = useTickets(projectId, completed?.id, completed !== undefined);
  const [seen, setSeen] = useState(() => readSeen(projectId));
  // The server's lane total; null on servers that only report what has loaded.
  const finished = lane.data?.pages[0]?.total ?? null;
  const milestone = playful ? milestoneToShow(finished, seen) : null;
  if (milestone === null) return null;
  const dismiss = () => {
    try {
      window.localStorage.setItem(seenKey(projectId), String(milestone));
    } catch {
      // Storage unavailable: hidden until the next load.
    }
    setSeen(milestone);
  };
  return (
    <div
      role="status"
      data-testid="project-milestone-note"
      className="flex items-center gap-3 rounded-xl border border-pink-300/60 bg-gradient-to-r from-pink-50 to-orange-50/60 px-3 py-2.5 dark:border-pink-400/30 dark:from-pink-500/10 dark:to-orange-400/5"
    >
      <OttoIcon className="otto-wiggle size-9 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">Your factory has finished {milestone} tickets</p>
        <p className="text-sm text-muted-foreground">A small milestone, from Otto.</p>
      </div>
      <Button
        variant="ghost"
        size="icon"
        aria-label="Dismiss milestone"
        className="size-7 shrink-0 rounded-full text-muted-foreground"
        onClick={dismiss}
      >
        <XIcon className="size-4" />
      </Button>
    </div>
  );
}
