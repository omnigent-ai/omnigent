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

export interface CommandGoalDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  readOnly: boolean;
  onStartGoal: (condition: string) => void;
}

/** Start a Claude goal by sending its native slash command as the next turn. */
export function CommandGoalDialog({
  open,
  onOpenChange,
  readOnly,
  onStartGoal,
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
      <DialogContent className="sm:max-w-lg">
        <form className="contents" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <TargetIcon className="size-4" />
              <span>Goal</span>
            </DialogTitle>
            <DialogDescription>
              Claude keeps working until this condition is met. Progress and completion appear in
              the conversation.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground" htmlFor="goal-condition">
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
            {error && <p className="text-sm text-destructive">{error}</p>}
          </div>

          <DialogFooter>
            <Button type="submit" disabled={readOnly} data-testid="goal-start">
              Start goal
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
