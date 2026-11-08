import { Fragment, type ReactNode } from "react";
import { useQueries } from "@tanstack/react-query";
import { TriangleAlertIcon } from "lucide-react";
import { scheduledTaskKey, useScheduledTask } from "@/hooks/useScheduledTasks";
import { automationPagePath } from "@/lib/automationFromSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link } from "@/lib/routing";
import { getScheduledTask, type ScheduledTask } from "@/lib/scheduledTasksApi";

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

/**
 * An automation's name, quoted and linked to its page; following it closes the
 * dialog. The name wraps as one unit and truncates rather than splitting.
 */
function AutomationLink({ task, onNavigate }: { task: ScheduledTask; onNavigate: () => void }) {
  const info = useServerInfo();
  return (
    <span className="inline-flex max-w-full items-baseline whitespace-nowrap">
      “
      <Link
        to={automationPagePath(info, task)}
        onClick={onNavigate}
        className="min-w-0 truncate font-medium text-primary underline underline-offset-2 hover:text-primary/80"
      >
        {task.name}
      </Link>
      ”
    </span>
  );
}

/** Delete-dialog warning for a session an automation runs from. */
export function AutomationSourceDeleteNotice({
  taskId,
  onNavigate,
}: {
  taskId: string;
  /** Closes the dialog when the automation's link is followed. */
  onNavigate: () => void;
}) {
  const { data: task } = useScheduledTask(taskId);
  return (
    <AutomationDeleteCallout
      title="This session runs an automation"
      testId="automation-source-delete-notice"
    >
      Deleting it also deletes the automation
      {task?.name ? (
        <>
          {" "}
          <AutomationLink task={task} onNavigate={onNavigate} />
        </>
      ) : null}
      .
    </AutomationDeleteCallout>
  );
}

/** Bulk-delete warning naming the automations that run from the selected sessions. */
export function AutomationSourcesDeleteNotice({
  taskIds,
  onNavigate,
}: {
  taskIds: string[];
  /** Closes the dialog when an automation's link is followed. */
  onNavigate: () => void;
}) {
  const queries = useQueries({
    queries: taskIds.map((id) => ({
      queryKey: scheduledTaskKey(id),
      queryFn: () => getScheduledTask(id),
      staleTime: 30_000,
    })),
  });
  const tasks = queries.flatMap((query) => (query.data?.name ? [query.data] : []));
  const single = taskIds.length === 1;
  const allNamed = tasks.length === taskIds.length;
  return (
    <AutomationDeleteCallout
      title={
        single
          ? "1 of these sessions runs an automation"
          : `${taskIds.length} of these sessions run automations`
      }
      testId="bulk-automation-source-delete-notice"
    >
      {single
        ? "Deleting it also deletes the automation"
        : `Deleting them also deletes ${allNamed ? "the automations" : "their automations"}`}
      {allNamed ? (
        <>
          {" "}
          <AutomationLinks tasks={tasks} onNavigate={onNavigate} />
        </>
      ) : null}
      .
    </AutomationDeleteCallout>
  );
}

/** Linked automation names as an English list, e.g. “A”, “B” and “C”. */
function AutomationLinks({
  tasks,
  onNavigate,
}: {
  tasks: ScheduledTask[];
  onNavigate: () => void;
}) {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const parts = new Intl.ListFormat("en", { type: "conjunction" }).formatToParts(
    tasks.map((task) => task.id),
  );
  return parts.map((part, index) => {
    const task = part.type === "element" ? byId.get(part.value) : undefined;
    return task ? (
      <AutomationLink key={task.id} task={task} onNavigate={onNavigate} />
    ) : (
      <Fragment key={index}>{part.value}</Fragment>
    );
  });
}
