import { PlayIcon, SquareIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import {
  isPlanComplete,
  type JournalEntry,
  pausedLabel,
  planGraphRevision,
  type StepProgress,
} from "@/lib/sessionJournal";
import { cn } from "@/lib/utils";

/** What a plan's run button asks of its main agent: stop or continue the work. */
export type PlanRunControl = "play" | "stop";

/**
 * The run button a plan shows: stop while its session works, play once the
 * agent stopped with tasks it could run and none waiting for the user, and
 * none once the plan is complete.
 */
export function planRunControl(
  progress: readonly StepProgress[],
  entries: readonly JournalEntry[],
  sessionWorking: boolean,
  planRevision = planGraphRevision(entries),
): PlanRunControl | null {
  if (sessionWorking && planRevision === undefined) return "stop";
  if (planRevision !== undefined && isPlanComplete(progress, planRevision)) return null;
  if (sessionWorking) return "stop";
  return pausedLabel(progress, entries, false, planRevision) ? "play" : null;
}

/** What a plan's run button shows: its control, or "Stopping…" from Stop until the run stops. */
export type PlanRunButtonState = PlanRunControl | "stopping";

/** Stop gives way to "Stopping…" while the run is stopping. */
export function planRunButtonState(
  control: PlanRunControl | null,
  stopping: boolean,
): PlanRunButtonState | null {
  return control === "stop" && stopping ? "stopping" : control;
}

/**
 * A plan's play or stop button, in the Tasks tab's header and on a workflow
 * run's row: asks the main agent to stop or resume the plan and its workers.
 * While the run stops, it's a disabled "Stopping…".
 */
export function PlanRunButton({
  control,
  onClick,
  disabled = false,
  size = "icon-sm",
  className,
}: {
  control: PlanRunButtonState;
  onClick?: () => void;
  disabled?: boolean;
  size?: "icon-xs" | "icon-sm";
  className?: string;
}) {
  if (control === "stopping") {
    return (
      <Button
        type="button"
        variant="ghost"
        size={size === "icon-xs" ? "xs" : "sm"}
        className={cn("shrink-0 text-muted-foreground", className)}
        disabled
        data-testid="plan-stopping"
      >
        <Spinner aria-hidden className="size-3.5" />
        Stopping…
      </Button>
    );
  }
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
