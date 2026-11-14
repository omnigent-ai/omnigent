import { useQuery } from "@tanstack/react-query";

import { WorkflowAutomationDeleteNotice } from "@/components/scheduled/AutomationSourceDeleteNotice";
import { splitTriggerPrompt } from "@/components/scheduled/triggerTemplates";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import { type SavedWorkflow, useDeleteWorkflow } from "@/hooks/useSavedWorkflows";
import { SCHEDULED_TASKS_KEY } from "@/hooks/useScheduledTasks";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { listScheduledTasks } from "@/lib/scheduledTasksApi";
import { parseSavedWorkflowMessage } from "@/lib/workflow";

export function DeleteWorkflowDialog({
  workflow,
  onClose,
  onDeleted,
}: {
  workflow: SavedWorkflow;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const info = useServerInfo();
  const automationsEnabled = info !== "loading" && info.automations_enabled !== false;
  const deleteWorkflow = useDeleteWorkflow();
  const automations = useQuery({
    queryKey: SCHEDULED_TASKS_KEY,
    queryFn: listScheduledTasks,
    enabled: automationsEnabled,
    // Recheck each opening; the list cache may predate a newly linked automation.
    refetchOnMount: "always",
    retry: false,
  });
  const linkedAutomations = (automations.data ?? []).filter(
    (task) =>
      parseSavedWorkflowMessage(splitTriggerPrompt(task.prompt).userPrompt)?.id === workflow.id,
  );
  const checking =
    info === "loading" || (automationsEnabled && (automations.isPending || automations.isFetching));
  const checkFailed = automationsEnabled && automations.isError;
  const canDelete = !checking && !checkFailed && !deleteWorkflow.isPending;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !deleteWorkflow.isPending) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete workflow?</DialogTitle>
          <DialogDescription>
            <span className="font-medium break-all">{workflow.name}</span> will be deleted. This
            cannot be undone.
          </DialogDescription>
        </DialogHeader>
        {checking ? (
          <p role="status" className="text-ui text-muted-foreground">
            Checking automations…
          </p>
        ) : checkFailed ? (
          <div role="alert" className="flex items-center gap-2 text-ui text-destructive">
            <span className="flex-1">Couldn’t check which automations use this workflow.</span>
            <Button variant="outline" size="sm" onClick={() => void automations.refetch()}>
              Retry
            </Button>
          </div>
        ) : automationsEnabled && linkedAutomations.length > 0 ? (
          <WorkflowAutomationDeleteNotice tasks={linkedAutomations} onNavigate={onClose} />
        ) : null}
        <DialogFooter className="border-t-0 bg-transparent">
          <Button variant="ghost" onClick={onClose} disabled={deleteWorkflow.isPending}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!canDelete}
            loading={deleteWorkflow.isPending}
            onClick={() =>
              deleteWorkflow.mutate(workflow.id, {
                onSuccess: () => {
                  onClose();
                  onDeleted?.();
                },
                onError: () => showToast(`Couldn’t delete “${workflow.name}”.`),
              })
            }
          >
            Delete workflow
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
