import { PlayIcon, SquareIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { type JournalEntry, pausedLabel, type StepProgress } from "@/lib/sessionJournal";
import { cn } from "@/lib/utils";

/** What a plan's run button does: stop the agent, or continue the plan. */
export type PlanRunControl = "play" | "stop";

/**
 * The run button a plan shows: stop while its session works, play once the
 * agent stopped with tasks it could run and none waiting for the user, and
 * none otherwise.
 */
export function planRunControl(
  progress: readonly StepProgress[],
  entries: readonly JournalEntry[],
  sessionWorking: boolean,
): PlanRunControl | null {
  if (sessionWorking) return "stop";
  return pausedLabel(progress, entries, false) ? "play" : null;
}

/**
 * A plan's play or stop button, in the Tasks tab's header and on a workflow
 * run's row: stop interrupts the agent, and play asks it to continue the plan.
 */
export function PlanRunButton({
  control,
  onClick,
  disabled = false,
  size = "icon-sm",
  className,
}: {
  control: PlanRunControl;
  onClick: () => void;
  disabled?: boolean;
  size?: "icon-xs" | "icon-sm";
  className?: string;
}) {
  const label = control === "stop" ? "Stop" : "Continue the plan";
  return (
    <Button
      type="button"
      variant="ghost"
      size={size}
      className={cn("shrink-0 text-muted-foreground hover:text-foreground", className)}
      aria-label={label}
      title={label}
      disabled={disabled}
      data-testid={control === "stop" ? "plan-stop" : "plan-continue"}
      onClick={onClick}
    >
      {control === "stop" ? (
        <SquareIcon className="size-3 fill-current" />
      ) : (
        <PlayIcon className={size === "icon-xs" ? "size-3.5" : "size-4"} />
      )}
    </Button>
  );
}
