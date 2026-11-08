import { useScheduledTask } from "@/hooks/useScheduledTasks";

/**
 * Delete-dialog warning for a session an automation runs from: deleting the
 * session also deletes the automation that starts every run from it.
 */
export function AutomationSourceDeleteNotice({ taskId }: { taskId: string }) {
  const { data: task } = useScheduledTask(taskId);
  return (
    <p className="text-sm text-muted-foreground" data-testid="automation-source-delete-notice">
      {task?.name ? (
        <>
          The automation <span className="font-medium text-foreground">“{task.name}”</span>
        </>
      ) : (
        "An automation"
      )}{" "}
      runs from this session. Deleting the session also deletes the automation.
    </p>
  );
}
