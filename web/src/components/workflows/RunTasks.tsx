import { FullscreenIcon } from "@/components/ai-elements/MermaidBlock";
import { Button } from "@/components/ui/button";
import type { StepProgress } from "@/lib/sessionJournal";
import { useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { ProgressStrip } from "@/shell/TasksPanel";
import { openTasksTab } from "@/store/sessionTasksActions";

/**
 * A run's tasks as the Tasks tab's timeline, above the row's link. A segment
 * opens that task in the run's session.
 */
export function RunTimeline({
  sessionId,
  progress,
  className,
}: {
  sessionId: string;
  progress: readonly StepProgress[];
  className?: string;
}) {
  const navigate = useNavigate();
  return (
    <div className={cn("relative z-10", className)}>
      <ProgressStrip
        progress={progress}
        fromPlan
        onSelect={(stepId) => {
          openTasksTab(sessionId, stepId);
          navigate(`/c/${encodeURIComponent(sessionId)}`);
        }}
      />
    </div>
  );
}

/** Opens a run on its workflow's graph, with the diagrams' "View fullscreen" icon. */
export function RunGraphButton({
  runName,
  onClick,
  className,
}: {
  /** Names the run for assistive technology, e.g. its session's title. */
  runName: string;
  onClick: () => void;
  className?: string;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      className={cn("z-10 shrink-0 text-muted-foreground hover:text-foreground", className)}
      aria-label={`View ${runName} on the graph`}
      title="View this run on the graph"
      data-testid="run-graph-button"
      onClick={onClick}
    >
      <FullscreenIcon />
    </Button>
  );
}
