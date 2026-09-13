import { useEffect, useRef, useState } from "react";
import { BotIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useChildSessions, type ChildSessionInfo } from "@/hooks/useChildSessions";

/**
 * Active-subagent tally for the ComposerWorkspaceBar, sitting beside the
 * background-task tally: a bot icon + count badge toggling a popover listing
 * each running sub-agent. Mirrors ``BackgroundTaskIndicator`` so the two
 * counts read as one family (background shells + delegated sub-agents).
 *
 * "Active" is the sub-agent's ``busy`` flag (a queued/in-progress task); a
 * finished child drops out of the count. Self-nulls at zero active sub-agents
 * and when there is no parent conversation (the landing window).
 */

function subagentLabel(child: ChildSessionInfo): string {
  const summary = child.task_summary?.trim();
  if (summary) return summary;
  const name = child.session_name?.trim();
  if (name) return name;
  const title = child.title?.trim();
  if (title) return title;
  return "Sub-agent";
}

export function SubagentTaskIndicator({ conversationId }: { conversationId: string | null }) {
  const { children } = useChildSessions(conversationId);
  const active = children.filter((child) => child.busy);
  const count = active.length;

  const [open, setOpen] = useState(false);
  // Why the popover closed last; only a session switch suppresses Radix's
  // close-autofocus, so ordinary Escape/outside closes keep restoring the
  // trigger. Mirrors BackgroundTaskIndicator so the switch effect can stay
  // free of an `open` dependency.
  const closeReasonRef = useRef<"session-change" | null>(null);
  const openRef = useRef(false);
  useEffect(() => {
    openRef.current = open;
  }, [open]);

  // An empty active list closes the popover; closing here (not only via the
  // null render below) keeps it from re-opening if a sub-agent later goes
  // busy again, and never moves focus itself.
  useEffect(() => {
    if (count <= 0) setOpen(false);
  }, [count]);
  // The tally is session-local: a conversation switch starts closed. Flag the
  // reason so the close never yanks focus to the new session's trigger.
  useEffect(() => {
    if (openRef.current) closeReasonRef.current = "session-change";
    setOpen(false);
  }, [conversationId]);

  const handleOpenChange = (next: boolean) => {
    if (next) closeReasonRef.current = null;
    setOpen(next);
  };

  if (count <= 0) return null;

  const countLabel = `${count} sub-agent${count === 1 ? "" : "s"} running`;

  return (
    <>
      {/* Polite tally so count changes are announced with the popover closed. */}
      <span role="status" className="sr-only">
        {countLabel}
      </span>
      <Popover open={open} onOpenChange={handleOpenChange}>
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="xs"
            data-testid="subagent-task-pill"
            aria-label={countLabel}
            className="shrink-0 px-0 md:px-2"
          >
            <BotIcon className="size-3.5" aria-hidden="true" />
            {count}
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
          <ul className="flex flex-col">
            {active.map((child) => {
              const label = subagentLabel(child);
              const tool = child.tool?.trim();
              const showTool = !!tool && tool !== label;
              return (
                <li key={child.id} className="flex items-start gap-2 px-1 py-2">
                  <span className="flex h-5 w-4 shrink-0 items-center justify-center text-muted-foreground">
                    <BotIcon className="size-4" aria-hidden="true" />
                  </span>
                  <span className="flex min-w-0 flex-1 flex-col gap-1">
                    <span className="truncate text-sm text-foreground" title={label}>
                      {label}
                    </span>
                    {showTool ? (
                      <span
                        className="truncate font-mono text-xs text-muted-foreground"
                        title={tool}
                      >
                        {tool}
                      </span>
                    ) : null}
                  </span>
                </li>
              );
            })}
          </ul>
        </PopoverContent>
      </Popover>
    </>
  );
}
