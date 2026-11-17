import { type FormEvent, useEffect, useState } from "react";
import { TargetIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import type { Goal } from "@/lib/goalApi";
import { cn } from "@/lib/utils";
import { goalStatusLabel, goalStatusTone } from "./goalUtils";

export interface CommandGoalDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  readOnly: boolean;
  onStartGoal: (condition: string) => void;
  backendLabel?: string;
  /** Goal the agent reports for this session; omitted when the backend cannot report one. */
  goal?: Goal | null;
  /** Clear the current goal; the Clear button is hidden when omitted. */
  onClearGoal?: () => void;
}

/**
 * Summarize a command-driven goal's usage, e.g. ``"3 checks · 1,200 tokens · 2 min"``.
 *
 * @param goal - Goal reported by the agent.
 * @returns Usage text, or an empty string before the first check.
 */
export function formatCommandGoalUsage(goal: Goal): string {
  const parts: string[] = [];
  const checks = goal.iterations ?? 0;
  if (checks > 0) parts.push(`${checks.toLocaleString()} ${checks === 1 ? "check" : "checks"}`);
  if (goal.tokensUsed > 0) parts.push(`${goal.tokensUsed.toLocaleString()} tokens`);
  const minutes = Math.floor(goal.timeUsedSeconds / 60);
  if (minutes > 0) parts.push(`${minutes.toLocaleString()} min`);
  return parts.join(" · ");
}

function CommandGoalSummary({ goal }: { goal: Goal }) {
  const tone = goalStatusTone(goal.status);
  const usage = formatCommandGoalUsage(goal);
  return (
    <div
      data-testid="goal-current"
      data-state={tone}
      className="space-y-1 rounded-lg border border-border bg-muted/30 p-3"
    >
      <div className="flex items-center justify-between gap-3 text-sm">
        <span
          className={cn(
            "font-medium text-foreground",
            tone === "paused" && "text-warning",
            tone === "failed" && "text-destructive",
          )}
        >
          {goalStatusLabel(goal.status)}
        </span>
        {usage && <span className="shrink-0 text-muted-foreground">{usage}</span>}
      </div>
      <p className="text-ui leading-5 wrap-anywhere whitespace-pre-wrap">{goal.objective}</p>
      {goal.statusDetail && (
        <p
          data-testid="goal-status-detail"
          className="text-ui leading-5 text-muted-foreground wrap-anywhere whitespace-pre-wrap"
        >
          {goal.statusDetail}
        </p>
      )}
      {tone === "paused" && (
        <p className="text-ui leading-5 text-muted-foreground">Send a message to continue.</p>
      )}
    </div>
  );
}

/**
 * Start a vendor goal by sending its native slash command as the next turn.
 *
 * When the backend reports goal state, the dialog also shows the current goal
 * and lets the user clear or replace it.
 */
export function CommandGoalDialog({
  open,
  onOpenChange,
  readOnly,
  onStartGoal,
  backendLabel = "The agent",
  goal,
  onClearGoal,
}: CommandGoalDialogProps) {
  const [condition, setCondition] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setCondition("");
      setError(null);
    }
  }, [open]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = condition.trim();
    if (!trimmed) {
      setError("Goal condition cannot be empty.");
      return;
    }
    onStartGoal(trimmed);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg max-h-[85dvh] grid-rows-[auto_1fr_auto]">
        <form className="contents" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <TargetIcon className="size-4" />
              <span>Goal</span>
            </DialogTitle>
            <DialogDescription>
              {backendLabel} keeps working until this condition is met. Progress and completion
              appear in the conversation.
            </DialogDescription>
          </DialogHeader>

          {/* Leave room for the field's focus ring inside the scrollable region. */}
          <div className="-m-1 min-h-0 space-y-1.5 overflow-y-auto p-1">
            {goal && (
              <div className="pb-2">
                <CommandGoalSummary goal={goal} />
              </div>
            )}
            <label className="text-sm font-medium text-muted-foreground" htmlFor="goal-condition">
              Completion condition
            </label>
            <Textarea
              id="goal-condition"
              value={condition}
              onChange={(event) => {
                setCondition(event.currentTarget.value);
                if (error !== null) setError(null);
              }}
              disabled={readOnly}
              maxLength={4000}
              className="min-h-28 resize-y"
              placeholder="All tests pass and the implementation is complete"
              data-testid="goal-condition"
            />
            {error && <p className="text-ui text-destructive">{error}</p>}
          </div>

          <DialogFooter>
            {goal && onClearGoal && (
              <Button
                type="button"
                variant="outline"
                disabled={readOnly}
                data-testid="goal-clear"
                onClick={() => {
                  onClearGoal();
                  onOpenChange(false);
                }}
              >
                Clear goal
              </Button>
            )}
            <Button type="submit" disabled={readOnly} data-testid="goal-start">
              {goal ? "Replace goal" : "Start goal"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
