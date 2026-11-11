/**
 * Share an existing automation's runs: edits only who each run's session is
 * shared with, from the automation's own page.
 */

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { AutomationShareFields, sameShares } from "@/components/scheduled/AutomationShareFields";
import { useUpdateScheduledTask } from "@/hooks/useScheduledTasks";
import type { ScheduledTask, ScheduledTaskShare } from "@/lib/scheduledTasksApi";

interface AutomationShareDialogProps {
  task: ScheduledTask;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function AutomationShareDialog({ task, open, onOpenChange }: AutomationShareDialogProps) {
  const saved = task.shareWith ?? [];
  const [shares, setShares] = useState<ScheduledTaskShare[]>(saved);
  const [error, setError] = useState<string | null>(null);
  const update = useUpdateScheduledTask();

  // Start each opening from the saved list, discarding an abandoned edit.
  useEffect(() => {
    if (!open) return;
    setShares(task.shareWith ?? []);
    setError(null);
  }, [open, task.shareWith]);

  function handleSave() {
    setError(null);
    update.mutate(
      { id: task.id, input: { shareWith: shares } },
      {
        onSuccess: () => onOpenChange(false),
        onError: (err: Error) => setError(err.message),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[480px]" data-testid="automation-share-dialog">
        <DialogHeader>
          <DialogTitle>Share runs</DialogTitle>
          <DialogDescription>
            {task.sessionMode === "continue"
              ? "Share the session this automation continues. Removing someone stops sharing later runs; it does not revoke the session."
              : "Share every session this automation's runs create. Removing someone affects later runs only."}
          </DialogDescription>
        </DialogHeader>
        <AutomationShareFields
          idPrefix="automation-share"
          value={shares}
          onChange={setShares}
          ownerId={task.ownerUserId}
          disabled={update.isPending}
        />
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            loading={update.isPending}
            disabled={sameShares(shares, saved)}
            data-testid="automation-share-save"
            componentId="tasks.scheduled.share_save"
          >
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
