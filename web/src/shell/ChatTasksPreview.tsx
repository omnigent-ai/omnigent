import { ChevronRightIcon, ListTodoIcon } from "lucide-react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import type { SessionTasksView } from "@/hooks/useSessionTasks";
import { finishedTaskCount, planName, taskPreviewText, WAITING_FOR_YOU } from "@/lib/sessionLedger";
import { cn } from "@/lib/utils";
import { openTasksTab } from "@/store/sessionTasksActions";

/** Whether the chat shows its task preview: the session has tasks to show. */
export function hasTaskPreview(tasks: SessionTasksView): boolean {
  return !tasks.loading && tasks.progress.length > 0;
}

/**
 * A one-line preview of the session's tasks, pinned to the top of the chat
 * thread: "Tasks 3/7 · Profiling the request". It counts finished tasks, failed
 * ones included, and names a task waiting for the user ("Waiting for you" in
 * pink), else the task in progress, or the next one; a click opens the Tasks
 * tab, which shows the full list. Hovering it names the plan.
 *
 * A flex sibling of the conversation viewport (not an overlay), so it shrinks
 * the scroll area rather than covering messages. Hidden while there are no tasks.
 */
export function ChatTasksPreview({
  conversationId,
  tasks,
  className,
}: {
  conversationId: string;
  tasks: SessionTasksView;
  className?: string;
}) {
  if (!hasTaskPreview(tasks)) return null;
  const { progress } = tasks;
  const finished = finishedTaskCount(progress);
  const text = taskPreviewText(progress, tasks.paused);
  const waiting = text?.startsWith(`${WAITING_FOR_YOU}:`) ?? false;
  const name = planName(tasks.plan);
  const button = (
    <button
      type="button"
      data-testid="tasks-preview"
      onClick={() => openTasksTab(conversationId)}
      aria-label={`Tasks${name ? ` for ${name}` : ""}, ${finished} of ${progress.length} finished${text ? `: ${text}` : ""}. Open the Tasks tab`}
      className={cn(
        "group mx-auto mb-1 flex w-full max-w-3xl cursor-pointer items-center gap-2 overflow-hidden rounded-xl border border-accent-foreground/20 bg-accent px-3 py-2 text-left text-ui text-accent-foreground shadow-sm transition-colors",
        "hover:border-accent-foreground/35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
      )}
    >
      <ListTodoIcon aria-hidden className="size-4 shrink-0" />
      <span className="shrink-0 font-medium">Tasks</span>
      <span className="shrink-0 tabular-nums opacity-70">
        {finished}/{progress.length}
      </span>
      {text && (
        <>
          <span aria-hidden className="shrink-0 opacity-40">
            ·
          </span>
          <span className="min-w-0 truncate opacity-90">
            {waiting ? (
              <>
                <span className="font-medium text-brand-accent">{WAITING_FOR_YOU}</span>
                {text.slice(WAITING_FOR_YOU.length)}
              </>
            ) : (
              text
            )}
          </span>
        </>
      )}
      <ChevronRightIcon
        aria-hidden
        className="ml-auto size-4 shrink-0 opacity-60 transition-transform group-hover:translate-x-0.5 group-hover:opacity-90 motion-reduce:transition-none"
      />
    </button>
  );
  return (
    // chat-tasks-preview is the stylesheet hook: index.css folds the native
    // shells' safe-area header shift into this row's top margin.
    <div className={cn("chat-tasks-preview shrink-0 px-3 md:px-4", className)}>
      {name ? (
        <TooltipProvider delayDuration={300}>
          <Tooltip>
            <TooltipTrigger asChild>{button}</TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-80 break-words">
              {name}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      ) : (
        button
      )}
    </div>
  );
}
