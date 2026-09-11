import { useEffect, useRef, useState } from "react";
import { SquareTerminalIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { BackgroundTaskInfo } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";

/**
 * Compact background-task tally trailing the ComposerWorkspaceBar: a terminal
 * icon + count badge toggling a non-modal popover that lists each running
 * shell (a dev server, a background shell), independent of the "Working…"
 * shimmer. The store's count is authoritative, so a count-only edge (older
 * runner, no per-shell detail) still gets the badge plus an honest
 * unavailable-details note instead of invented rows.
 */

function taskLabel(task: BackgroundTaskInfo): string {
  const description = task.description?.trim();
  if (description) return description;
  const command = task.command?.trim();
  if (command) return command;
  return "Background task";
}

export function BackgroundTaskIndicator() {
  const bgCount = useChatStore((s) => s.backgroundTaskCount);
  const bgTasks = useChatStore((s) => s.backgroundTasks);
  const conversationId = useChatStore((s) => s.conversationId);
  const [open, setOpen] = useState(false);
  // Why the popover closed last; only a session switch suppresses Radix's
  // close-autofocus, so ordinary Escape/outside closes keep restoring the
  // trigger. Mirrors let the switch effect stay free of an `open` dependency
  // (depending on it would close the panel the moment it opened).
  const closeReasonRef = useRef<"session-change" | null>(null);
  const openRef = useRef(false);
  useEffect(() => {
    openRef.current = open;
  }, [open]);

  // An explicit zero closes the popover; closing here (not only via the null
  // render below) keeps it from re-opening if the count later recovers, and
  // never moves focus itself.
  useEffect(() => {
    if (bgCount <= 0) setOpen(false);
  }, [bgCount]);
  // The tally is session-local: a conversation switch starts closed. Flag the
  // reason so the close never yanks focus to the new session's trigger.
  useEffect(() => {
    if (openRef.current) closeReasonRef.current = "session-change";
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
      <Popover open={open} onOpenChange={handleOpenChange}>
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="xs"
            data-testid="background-task-pill"
            aria-label={`${countLabel} still running`}
            className="ml-auto shrink-0"
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
          onCloseAutoFocus={(event) => {
            if (closeReasonRef.current === "session-change") {
              // Closed by a conversation switch, not a user gesture: never
              // move focus to the new session's trigger.
              event.preventDefault();
              closeReasonRef.current = null;
            }
          }}
          className="max-h-[min(24rem,var(--radix-popover-content-available-height))] w-[min(25rem,calc(100vw-2rem))] overflow-y-auto p-2"
        >
          {displayedTasks.length > 0 ? (
            <>
              <ul className="flex flex-col">
                {displayedTasks.map((task, i) => {
                  const label = taskLabel(task);
                  const command = task.command?.trim();
                  const showCommand = !!command && command !== label;
                  return (
                    <li key={task.id ?? i} className="flex items-start gap-2 px-1 py-2">
                      <span className="flex h-5 w-4 shrink-0 items-center justify-center text-muted-foreground">
                        <SquareTerminalIcon className="size-4" aria-hidden="true" />
                      </span>
                      <span className="flex min-w-0 flex-1 flex-col gap-1">
                        <span className="truncate text-sm text-foreground" title={label}>
                          {label}
                        </span>
                        {showCommand ? (
                          <span
                            className="truncate font-mono text-xs text-muted-foreground"
                            title={command}
                          >
                            {command}
                          </span>
                        ) : null}
                      </span>
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
