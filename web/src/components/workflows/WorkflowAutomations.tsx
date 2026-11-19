import { useMemo } from "react";
import { ClockIcon, Loader2Icon } from "lucide-react";

import { isTriggerPrompt } from "@/components/scheduled/triggerTemplates";
import { useScheduledTasks } from "@/hooks/useScheduledTasks";
import { automationPagePath } from "@/lib/automationFromSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link } from "@/lib/routing";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";
import { describeSchedule, formatNextRunAt, formatRunTimestamp } from "@/lib/scheduleText";
import { cn } from "@/lib/utils";
import { automationsUsingWorkflow } from "@/lib/workflow";

/** A workflow page's list of the automations that run the workflow. */
export function WorkflowAutomations({ workflowId, now }: { workflowId: string; now: Date }) {
  const { data, isError } = useScheduledTasks();
  const automations = useMemo(
    () => automationsUsingWorkflow(data ?? [], workflowId),
    [data, workflowId],
  );
  return (
    <section className="mt-6">
      <h2 className="mb-2 text-sm font-medium text-muted-foreground">Automations</h2>
      {/* A failed poll keeps the list it already loaded. */}
      {data ? (
        automations.length > 0 ? (
          <ul className="flex flex-col gap-1" data-testid="workflow-automations">
            {automations.map((automation) => (
              <WorkflowAutomationRow key={automation.id} automation={automation} now={now} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground" data-testid="workflow-automations-empty">
            No automations yet.
          </p>
        )
      ) : isError ? (
        <p className="text-sm text-muted-foreground" data-testid="workflow-automations-error">
          Couldn’t load automations.
        </p>
      ) : (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading automations…
        </div>
      )}
    </section>
  );
}

/** One automation: a link to its page, over its schedule and when it last ran. */
function WorkflowAutomationRow({ automation, now }: { automation: ScheduledTask; now: Date }) {
  const info = useServerInfo();
  const paused = automation.state === "paused";
  const failed = automation.lastRunStatus === "failed";
  // As on the Automations page, a trigger runs when new items match, not on a schedule.
  const triggerMode = automation.sessionMode === "continue" && isTriggerPrompt(automation.prompt);
  const nextRun = triggerMode ? null : formatNextRunAt(automation.nextRunAt, now);
  const lastRun = formatRunTimestamp(automation.lastRunAt, now);
  return (
    <li
      className="relative rounded-lg px-2 py-2.5 transition-colors hover:bg-muted/60 has-[a:focus-visible]:bg-muted/60"
      data-testid="workflow-automation"
      data-state={automation.state}
    >
      <div className="flex items-start gap-3">
        <span className="flex size-5 shrink-0 items-center justify-center text-muted-foreground">
          <ClockIcon className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            {/* The link covers the row. */}
            <Link
              to={automationPagePath(info, automation)}
              className="min-w-0 truncate text-sm leading-5 after:absolute after:inset-0 after:rounded-lg after:content-[''] focus-visible:outline-none"
            >
              {automation.name}
            </Link>
            {paused && (
              <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                Paused
              </span>
            )}
          </div>
          <p
            className="mt-0.5 text-xs text-muted-foreground"
            data-testid="workflow-automation-schedule"
          >
            {triggerMode
              ? "Runs when new items match"
              : describeSchedule(automation.rrule, automation.timezone)}
            {nextRun && ` · Next run ${nextRun}`}
            {lastRun && (
              <>
                {" · "}
                <span className={cn(failed && "text-status-red")}>
                  {failed ? "Last run failed" : "Last run"} {lastRun}
                </span>
              </>
            )}
          </p>
        </div>
      </div>
    </li>
  );
}
