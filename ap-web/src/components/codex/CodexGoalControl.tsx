import { useEffect, useState } from "react";
import { TargetIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { CodexGoal } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";
import { CodexGoalDialog } from "./CodexGoalDialog";
import { formatCodexGoalStatus } from "./codexGoalUtils";

interface CodexGoalControlProps {
  conversationId: string | null;
  readOnly: boolean;
  goal: CodexGoal | null;
  onGoalChange: (goal: CodexGoal | null) => void;
}

/** Toolbar button plus dialog for Codex-native goal controls. */
export function CodexGoalControl({
  conversationId,
  readOnly,
  goal,
  onGoalChange,
}: CodexGoalControlProps) {
  const [open, setOpen] = useState(false);

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
            aria-label={goal ? "View Codex goal" : "Set Codex goal"}
            data-testid="codex-goal-toggle"
            data-active={goal ? "true" : undefined}
            onClick={() => setOpen(true)}
          >
            <TargetIcon className="size-3.5" />
            <span>Goal</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent>{goal ? "View Codex goal" : "Set Codex goal"}</TooltipContent>
      </Tooltip>
      <CodexGoalDialog
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

/** Compact status-line indicator for the current Codex goal. */
export function CodexGoalStatusPill({ goal }: { goal: CodexGoal }) {
  return (
    <span
      data-testid="composer-goal-mode"
      className="inline-flex items-center gap-1 text-xs font-medium text-foreground"
    >
      <TargetIcon className="size-3.5 shrink-0" />
      <span>Goal {formatCodexGoalStatus(goal.status)}</span>
    </span>
  );
}
