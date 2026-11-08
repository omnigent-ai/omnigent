import type { ReactNode } from "react";
import { useQueries } from "@tanstack/react-query";
import { TriangleAlertIcon } from "lucide-react";
import { scheduledTaskKey, useScheduledTask } from "@/hooks/useScheduledTasks";
import { getScheduledTask } from "@/lib/scheduledTasksApi";
import { describeSchedule } from "@/lib/scheduleText";

/**
 * Amber callout a delete dialog shows when an automation runs from a session it
 * deletes, because the automation is deleted with the session.
 */
function AutomationDeleteCallout({
  title,
  children,
  testId,
}: {
  title: string;
  children: ReactNode;
  testId: string;
}) {
  return (
    <div
      className="flex gap-2.5 rounded-md border border-warning/40 bg-warning/10 p-3 text-ui text-foreground"
      data-testid={testId}
    >
      <TriangleAlertIcon aria-hidden className="mt-0.5 size-4 shrink-0 text-warning" />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className="font-medium">{title}</p>
        <p>{children}</p>
      </div>
    </div>
  );
}

/** A schedule mid-sentence, e.g. "every day at 9:00 AM"; a raw rule stays as is. */
function scheduleClause(rrule: string): string {
  const text = describeSchedule(rrule);
  return /^[A-Z][a-z]/.test(text) ? text[0].toLowerCase() + text.slice(1) : text;
}

/** Delete-dialog warning for a session an automation runs from. */
export function AutomationSourceDeleteNotice({ taskId }: { taskId: string }) {
  const { data: task } = useScheduledTask(taskId);
  return (
    <AutomationDeleteCallout
      title="This session runs an automation"
      testId="automation-source-delete-notice"
    >
      {task?.name ? (
        <>
          Deleting it also deletes <span className="font-medium">“{task.name}”</span>
          {task.rrule ? `, which runs ${scheduleClause(task.rrule)}` : ""}.
        </>
      ) : (
        "Deleting it also deletes the automation."
      )}
    </AutomationDeleteCallout>
  );
}

/** Bulk-delete warning naming the automations that run from the selected sessions. */
export function AutomationSourcesDeleteNotice({ taskIds }: { taskIds: string[] }) {
  const tasks = useQueries({
    queries: taskIds.map((id) => ({
      queryKey: scheduledTaskKey(id),
      queryFn: () => getScheduledTask(id),
      staleTime: 30_000,
    })),
  });
  const names = tasks.flatMap((query) => (query.data?.name ? [`“${query.data.name}”`] : []));
  const single = taskIds.length === 1;
  return (
    <AutomationDeleteCallout
      title={
        single
          ? "1 of these sessions runs an automation"
          : `${taskIds.length} of these sessions run automations`
      }
      testId="bulk-automation-source-delete-notice"
    >
      {names.length === taskIds.length
        ? `Deleting ${single ? "it" : "them"} also deletes ${new Intl.ListFormat("en", { type: "conjunction" }).format(names)}.`
        : `Deleting ${single ? "it also deletes its automation" : "them also deletes their automations"}.`}
    </AutomationDeleteCallout>
  );
}
