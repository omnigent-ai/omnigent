import { useEffect, useId, useRef, useState } from "react";
import { ChevronRightIcon, SquareTerminalIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { BackgroundTaskInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

function taskLabel(task: BackgroundTaskInfo): string {
  const description = task.description?.trim();
  if (description) return description;
  const command = task.command?.trim();
  if (command) return command;
  return "Background task";
}

function TaskCommand({ command, label }: { command: string; label: string }) {
  const [expanded, setExpanded] = useState(false);
  const [clipped, setClipped] = useState(false);
  const commandRef = useRef<HTMLDivElement>(null);
  const commandId = useId();
  const asLabel = command === label;

  useEffect(() => {
    const element = commandRef.current;
    if (!element || expanded) return;
    const measure = () => setClipped(element.scrollHeight > element.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [command, expanded]);

  return (
    <div className="flex min-w-0 items-start gap-1">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        {!asLabel && (
          <span className="text-sm text-foreground [overflow-wrap:anywhere]">{label}</span>
        )}
        <div
          ref={commandRef}
          id={commandId}
          data-testid="background-task-command"
          className={cn(
            "whitespace-pre-wrap [overflow-wrap:anywhere]",
            asLabel ? "text-sm text-foreground" : "font-mono text-xs text-muted-foreground",
            !expanded && "line-clamp-2",
          )}
        >
          {command}
        </div>
      </div>
      {(clipped || expanded) && (
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={expanded ? "Collapse command" : "Expand command"}
          aria-controls={commandId}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
          className="shrink-0 text-muted-foreground"
        >
          <ChevronRightIcon
            aria-hidden="true"
            className={cn("size-3.5", expanded && "rotate-90")}
          />
        </Button>
      )}
    </div>
  );
}

/** Running shells and monitors, independent of foreground work; the count is authoritative. */
export function BackgroundTaskIndicator() {
  const bgCount = useChatStore((s) => s.backgroundTaskCount);
  const bgTasks = useChatStore((s) => s.backgroundTasks);
  const conversationId = useChatStore((s) => s.conversationId);
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  // Escape returns to the trigger; session switches must never move focus.
  const closeReasonRef = useRef<"session-change" | "escape" | null>(null);

  // An explicit zero closes the popover; closing here (not only via the null
  // render below) keeps it from re-opening if the count later recovers, and
  // never moves focus itself.
  useEffect(() => {
    if (bgCount <= 0) setOpen(false);
  }, [bgCount]);
  // The tally is session-local: a conversation switch starts closed. Flag the
  // reason so the close never yanks focus to the new session's trigger.
  useEffect(() => {
    closeReasonRef.current = "session-change";
    setOpen(false);
  }, [conversationId]);

  const handleOpenChange = (next: boolean) => {
    // A fresh open consumes any stale close reason, so the next ordinary
    // close restores focus normally.
    if (next) closeReasonRef.current = null;
    setOpen(next);
  };

  if (bgCount <= 0) return null;

  const countLabel = `${bgCount} background task${bgCount === 1 ? "" : "s"}`;
  // The count is authoritative in both directions: an over-long detail list
  // is clamped, a short one is acknowledged as partially unavailable.
  const displayedTasks = bgTasks.slice(0, bgCount);
  const undetailed = bgCount - displayedTasks.length;

  return (
    <>
      {/* Polite tally so count changes are announced with the popover
          closed, replacing the old pill's role="status". */}
      <span role="status" className="sr-only">
        {countLabel} still running
      </span>
      <Popover modal={false} open={open} onOpenChange={handleOpenChange}>
        <PopoverTrigger asChild>
          <Button
            ref={triggerRef}
            type="button"
            variant="ghost"
            size="xs"
            data-testid="background-task-pill"
            aria-label={`${countLabel} still running`}
            className="shrink-0 px-0 font-normal tabular-nums text-muted-foreground md:px-2"
          >
            <SquareTerminalIcon className="size-3.5" aria-hidden="true" />
            {bgCount}
          </Button>
        </PopoverTrigger>
        <PopoverContent
          side="top"
          align="end"
          collisionPadding={8}
          aria-label={countLabel}
          onEscapeKeyDown={() => {
            if (open) closeReasonRef.current = "escape";
          }}
          onInteractOutside={() => {
            // A later outside interaction owns focus, including during the exit animation.
            if (closeReasonRef.current === "escape") closeReasonRef.current = null;
          }}
          onCloseAutoFocus={(event) => {
            if (closeReasonRef.current === "session-change") {
              // Closed by a conversation switch, not a user gesture: never
              // move focus to the new session's trigger.
              event.preventDefault();
            } else if (closeReasonRef.current === "escape") {
              // A rapid reopen can retain Radix's previous outside-click state.
              event.preventDefault();
              triggerRef.current?.focus();
            }
            closeReasonRef.current = null;
          }}
          className="max-h-[min(24rem,var(--radix-popover-content-available-height))] w-[min(25rem,calc(100vw-2rem))] overflow-y-auto p-2"
        >
          {displayedTasks.length > 0 ? (
            <>
              <ul className="flex flex-col">
                {displayedTasks.map((task, i) => {
                  const label = taskLabel(task);
                  const command = task.command?.trim();
                  return (
                    <li key={task.id ?? i} className="flex items-start gap-2 px-1 py-2">
                      <span className="flex h-5 w-4 shrink-0 items-center justify-center text-muted-foreground">
                        <SquareTerminalIcon className="size-4" aria-hidden="true" />
                      </span>
                      <div className="flex min-w-0 flex-1 flex-col gap-1">
                        {command ? (
                          <TaskCommand key={`${open}:${command}`} command={command} label={label} />
                        ) : (
                          <span className="text-sm text-foreground [overflow-wrap:anywhere]">
                            {label}
                          </span>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
              {undetailed > 0 ? (
                <p className="border-t border-border/60 px-1 pb-1 pt-2 text-sm text-muted-foreground">
                  +{undetailed} more — details unavailable
                </p>
              ) : null}
            </>
          ) : (
            <p className="px-1 py-2 text-sm text-muted-foreground">
              {countLabel} — details unavailable
            </p>
          )}
        </PopoverContent>
      </Popover>
    </>
  );
}
