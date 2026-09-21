import { useEffect, useRef, useState, type MouseEvent } from "react";
import { BotIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useChildSessions, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { Link, useLocation } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { childStatus } from "@/shell/subagentStatus";

/**
 * Actionable sub-agent tally for the ComposerWorkspaceBar, sitting beside the
 * background-task tally: a bot icon + count badge toggling a popover listing
 * each active, parked, or errored sub-agent. Mirrors ``BackgroundTaskIndicator`` so the two
 * counts read as one family (background shells + delegated sub-agents).
 */

type IndicatorState = "active" | "parked" | "error";

interface IndicatorChild {
  child: ChildSessionInfo;
  state: IndicatorState;
  statusLabel: string;
}

function subagentLabel(child: ChildSessionInfo): string {
  const summary = child.task_summary?.trim();
  if (summary) return summary;
  const name = child.session_name?.trim();
  if (name) return name;
  const title = child.title?.trim();
  if (title) return title;
  return "Sub-agent";
}

function indicatorChild(child: ChildSessionInfo): IndicatorChild | null {
  const status = childStatus(child);
  if (status.activity === "launching" || status.activity === "working") {
    return { child, state: "active", statusLabel: status.label };
  }
  if (status.activity === "awaiting") {
    return { child, state: "parked", statusLabel: status.label };
  }
  if (status.activity === "failed" || status.activity === "disconnected") {
    return { child, state: "error", statusLabel: status.label };
  }
  return null;
}

function statusSummary(items: IndicatorChild[]): string {
  const active = items.filter((item) => item.state === "active").length;
  const parked = items.filter((item) => item.state === "parked").length;
  const errors = items.filter((item) => item.state === "error").length;
  const total = items.length;
  const parts = [
    active > 0 ? `${active} active` : null,
    parked > 0 ? `${parked} awaiting input` : null,
    errors > 0 ? `${errors} need${errors === 1 ? "s" : ""} attention` : null,
  ].filter(Boolean);
  return `${total} sub-agent${total === 1 ? "" : "s"}: ${parts.join(", ")}`;
}

function linkSearch(search: string): string {
  const params = new URLSearchParams(search);
  for (const key of ["file", "diff", "comment", "view"]) params.delete(key);
  const next = params.toString();
  return next ? `?${next}` : "";
}

function SubagentStateIndicator({ state, label }: { state: IndicatorState; label: string }) {
  if (state === "active") {
    return (
      <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
        <span
          role="status"
          aria-label={label}
          className="size-3.5 shrink-0 animate-spin rounded-full motion-reduce:animate-none"
          style={{
            animationDuration: "1.6s",
            background: "conic-gradient(from 0deg, transparent 0deg, currentColor 360deg)",
            WebkitMask:
              "radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 2px))",
            mask: "radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 2px))",
          }}
        />
        <span aria-hidden="true">{label}</span>
      </span>
    );
  }

  return (
    <span
      role="status"
      aria-label={label}
      className={cn(
        "flex shrink-0 items-center gap-1.5 text-xs",
        state === "parked" ? "text-warning" : "text-destructive",
      )}
    >
      <span className="size-1.5 rounded-full bg-current" aria-hidden="true" />
      {label}
    </span>
  );
}

function SubagentNavigationRow({
  item: { child, state, statusLabel },
  onNavigate,
}: {
  item: IndicatorChild;
  onNavigate: (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const search = linkSearch(useLocation().search);
  const label = subagentLabel(child);
  const tool = child.tool?.trim();
  const showTool = !!tool && tool !== label;

  return (
    <li>
      <Link
        to={{ pathname: `/c/${child.id}`, search }}
        componentId="composer-subagent-indicator-row"
        aria-label={`Open ${label} sub-agent`}
        onClick={onNavigate}
        className="flex items-start gap-2 rounded-lg px-1 py-2 hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <span className="flex h-5 w-5 shrink-0 items-center justify-center text-muted-foreground">
          <BotIcon className="size-4" aria-hidden="true" />
        </span>
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex min-w-0 items-center gap-2">
            <span className="min-w-0 flex-1 truncate text-sm text-foreground" title={label}>
              {label}
            </span>
            <SubagentStateIndicator state={state} label={statusLabel} />
          </span>
          {showTool ? (
            <span className="truncate font-mono text-xs text-muted-foreground" title={tool}>
              {tool}
            </span>
          ) : null}
        </span>
      </Link>
    </li>
  );
}

export function SubagentTaskIndicator({ conversationId }: { conversationId: string | null }) {
  const { children } = useChildSessions(conversationId);
  const items = children
    .map(indicatorChild)
    .filter((item): item is IndicatorChild => item !== null);
  const count = items.length;

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

  const countLabel = statusSummary(items);
  const triggerState: IndicatorState = items.some((item) => item.state === "error")
    ? "error"
    : items.some((item) => item.state === "parked")
      ? "parked"
      : "active";

  const handleNavigate = (event: MouseEvent<HTMLAnchorElement>) => {
    if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) setOpen(false);
  };

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
            data-state={triggerState}
            aria-label={countLabel}
            className={cn(
              "shrink-0 gap-1 px-1 md:px-2",
              triggerState === "error" && "text-destructive hover:text-destructive",
              triggerState === "parked" && "text-warning hover:text-warning",
            )}
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
            {items.map((item) => (
              <SubagentNavigationRow key={item.child.id} item={item} onNavigate={handleNavigate} />
            ))}
          </ul>
        </PopoverContent>
      </Popover>
    </>
  );
}
