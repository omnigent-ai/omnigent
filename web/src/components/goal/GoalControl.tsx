import { useEffect, useState } from "react";
import { TargetIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Goal } from "@/lib/goalApi";
import { cn } from "@/lib/utils";
import { GoalDialog } from "./GoalDialog";
import { formatGoalStatus } from "./goalUtils";

interface GoalControlProps {
  conversationId: string | null;
  readOnly: boolean;
  goal: Goal | null;
  onGoalChange: (goal: Goal | null) => void;
  /** Optional backend name used by the current Codex-only tooltip. */
  backendLabel?: string;
}

/** Toolbar button plus dialog for a goal-capable session. */
export function GoalControl({
  conversationId,
  readOnly,
  goal,
  onGoalChange,
  backendLabel,
}: GoalControlProps) {
  const [open, setOpen] = useState(false);
  const goalName = backendLabel ? `${backendLabel} goal` : "goal";

  useEffect(() => {
    if (!conversationId) setOpen(false);
  }, [conversationId]);

  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            size="sm"
            variant={goal ? "secondary" : "ghost"}
            className={cn(
              "h-9 gap-1.5 px-2 text-xs md:h-8",
              goal && "border border-ring/30 text-foreground",
            )}
            disabled={!conversationId}
            aria-pressed={goal != null}
            aria-label={goal ? `View ${goalName}` : `Set ${goalName}`}
            data-testid="goal-toggle"
            data-active={goal ? "true" : undefined}
            onClick={() => setOpen(true)}
          >
            <TargetIcon className="size-3.5" />
            <span>Goal</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent>{goal ? `View ${goalName}` : `Set ${goalName}`}</TooltipContent>
      </Tooltip>
      <GoalDialog
        open={open}
        onOpenChange={setOpen}
        conversationId={conversationId}
        readOnly={readOnly}
        goal={goal}
        onGoalChange={onGoalChange}
      />
    </>
  );
}

/** Compact status-line indicator for the current goal. */
export function GoalStatusPill({ goal }: { goal: Goal }) {
  return (
    <span
      data-testid="composer-goal-mode"
      className="inline-flex items-center gap-1 text-xs font-medium text-foreground"
    >
      <TargetIcon className="size-3.5 shrink-0" />
      <span>Goal {formatGoalStatus(goal.status)}</span>
    </span>
  );
}
