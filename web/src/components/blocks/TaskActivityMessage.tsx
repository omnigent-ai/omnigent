import { createContext, useContext } from "react";
import { ListChecksIcon } from "lucide-react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  planChangeText,
  readTaskActivity,
  taskMarkerId,
  taskMarkerLabel,
  type TaskActivity,
} from "@/lib/taskActivity";
import { cn } from "@/lib/utils";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useChatStore } from "@/store/chatStore";
import { openTasksTab } from "@/store/sessionTasksActions";
import { SubagentActivityMessage } from "./SubagentActivityMessage";

/** The chat's session and its current tasks, which a marker links to while they last. */
export interface TaskMarkersValue {
  conversationId: string;
  /** IDs of the tasks in the Tasks tab now: the plan's steps, or the agent's own list. */
  taskIds: ReadonlySet<string>;
}

const TaskMarkersContext = createContext<TaskMarkersValue | null>(null);

/** Lets the transcript's task markers link to the Tasks tab. Without it they're plain text. */
export const TaskMarkersProvider = TaskMarkersContext.Provider;

const LINK =
  "cursor-pointer rounded-sm font-normal text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/** A centered marker in the chat: a sub-agent's or a task's start and end, or a plan change. */
export function ActivityNotice({ data }: { data: Record<string, unknown> }) {
  const task = readTaskActivity(data);
  return task ? <TaskActivityMessage activity={task} /> : <SubagentActivityMessage data={data} />;
}

/**
 * Where a task started, finished, failed or began waiting for the user, or a
 * change to the plan, in the sub-agent marker's form. The task's name opens
 * the Tasks tab at it while it's still a task there.
 */
export function TaskActivityMessage({ activity }: { activity: TaskActivity }) {
  const markers = useContext(TaskMarkersContext);
  const workflowsEnabled = isFeatureEnabled(useServerInfo(), "workflows");
  const id = taskMarkerId(activity);
  const flashing = useChatStore((state) => state.flashItemId === id);
  if (!workflowsEnabled && (activity.kind === "plan" || !activity.native)) return null;
  let content;
  if (activity.kind === "plan") {
    const text = planChangeText(activity);
    content = markers ? (
      <button type="button" className={LINK} onClick={() => openTasksTab(markers.conversationId)}>
        {text}
      </button>
    ) : (
      text
    );
  } else {
    const label = taskMarkerLabel(activity.status);
    const current = markers?.taskIds.has(activity.stepId) ?? false;
    let name;
    if (markers && current) {
      name = (
        <button
          type="button"
          className={LINK}
          onClick={() => openTasksTab(markers.conversationId, activity.stepId)}
        >
          {activity.name}
        </button>
      );
    } else if (markers) {
      // Its plan dropped it: the marker stays where it happened but links nowhere.
      name = (
        <TooltipProvider delayDuration={300}>
          <Tooltip>
            <TooltipTrigger asChild>
              <span tabIndex={0} className="rounded-sm text-foreground focus-visible:outline-none">
                {activity.name}
              </span>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-80 break-words">
              {activity.text ? `${activity.text} · ` : ""}
              {activity.native ? "No longer in the task list" : "No longer in the plan"}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      );
    } else {
      name = <span className="text-foreground">{activity.name}</span>;
    }
    content = (
      <>
        <span className={cn(activity.status === "needs_input" && "text-brand-accent")}>
          {label}
        </span>{" "}
        {name}
      </>
    );
  }
  return (
    <div
      className="not-prose flex w-full items-center gap-2 py-2 text-sm text-muted-foreground"
      data-testid="task-activity"
      data-message-id={id}
      data-kind={activity.kind}
      data-status={activity.kind === "task" ? activity.status : undefined}
      data-step-id={activity.kind === "task" ? activity.stepId : undefined}
    >
      <span aria-hidden="true" className="min-w-0 flex-1 border-t border-dashed border-border" />
      <div
        className={cn(
          "flex min-w-0 max-w-[85%] items-center gap-1.5 rounded-full border border-transparent px-3 py-1 text-center transition-colors hover:border-border focus-within:border-border",
          // The highlight lands at once, with the scroll that reveals it, then fades.
          flashing ? "border-border bg-session-active/12 duration-0" : "duration-500",
        )}
      >
        <ListChecksIcon aria-hidden="true" className="size-4 shrink-0" />
        <span className="min-w-0 break-words">{content}</span>
      </div>
      <span aria-hidden="true" className="min-w-0 flex-1 border-t border-dashed border-border" />
    </div>
  );
}
