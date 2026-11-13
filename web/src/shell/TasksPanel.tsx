// The Tasks tab: where the session's tasks stand, one row each. Tasks are a
// plan's steps, or the agent's own task list when the session has no plan.

import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  CheckIcon,
  ChevronRightIcon,
  EllipsisIcon,
  ListChecksIcon,
  Loader2Icon,
  MessageCircleQuestionMarkIcon,
  PlayIcon,
  RotateCcwIcon,
  SquareIcon,
  WorkflowIcon,
  XIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useNow } from "@/hooks/useNow";
import { useSavedWorkflows } from "@/hooks/useSavedWorkflows";
import { useSessionTasks } from "@/hooks/useSessionTasks";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  absoluteTime,
  formatDuration,
  formatMessageTimestamp,
  relativeTime,
} from "@/lib/relativeTime";
import {
  finishedTaskCount,
  isRestart,
  linkedToPrevious,
  planName,
  planTally,
  planWorkflowId,
  statusOf,
  stepStatusLabel,
  textOf,
  WAITING_FOR_YOU,
  type JournalEntry,
  type JournalPlanStep,
  type PausedLabel,
  type StepProgress,
  type StepState,
  type StepStatus,
} from "@/lib/sessionJournal";
import { messageDeepLinkParams } from "@/lib/messageDeepLink";
import { Link, useSearchParams } from "@/lib/routing";
import { taskMarkerTarget } from "@/lib/taskActivity";
import { cn } from "@/lib/utils";
import type { TaskAction } from "@/lib/workflow";
import { PlanRunButton } from "@/shell/PlanRunButton";
import { useSessionTasksActions } from "@/store/sessionTasksActions";

const NO_ENTRIES: JournalEntry[] = [];

export const STATE_LABELS: Record<StepState, string> = {
  waiting: "Not started",
  ready: "Ready",
  running: "Running",
  stopped: "Stopped",
  needs_input: WAITING_FOR_YOU,
  done: "Done",
  failed: "Failed",
};

export const SEGMENT_COLORS: Record<StepState, string> = {
  waiting: "bg-foreground/10",
  ready: "bg-session-active/25",
  running: "bg-session-active animate-pulse motion-reduce:animate-none",
  stopped: "bg-status-gray",
  needs_input: "bg-brand-accent",
  done: "bg-success",
  failed: "bg-destructive",
};

function clock(seconds: number): string {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** "3m ago", kept fresh by the shared clock. */
function Ago({ seconds }: { seconds: number }) {
  const now = useNow();
  const ago = relativeTime(seconds * 1000, now.getTime());
  return (
    <time dateTime={new Date(seconds * 1000).toISOString()} title={absoluteTime(seconds * 1000)}>
      {ago === "now" ? "just now" : `${ago} ago`}
    </time>
  );
}

/** A running step's time so far, counting up every second. */
function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    // Twice a second, so the count turns over close to each whole second.
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, []);
  return <>{formatDuration(now / 1000 - since)}</>;
}

/** A step's state as a small circle: numbered while it waits, filled once it ends. */
export function StepGlyph({ state, number }: { state: StepState; number?: number }) {
  const shape = "flex size-[18px] shrink-0 items-center justify-center rounded-full";
  const label = STATE_LABELS[state];
  switch (state) {
    case "running":
      return (
        <span role="img" aria-label={label} className={cn(shape, "text-session-active")}>
          <Loader2Icon
            className="size-[18px] animate-spin motion-reduce:animate-none"
            strokeWidth={2.25}
          />
        </span>
      );
    case "needs_input":
      return (
        <span role="img" aria-label={label} className={cn(shape, "text-brand-accent")}>
          <MessageCircleQuestionMarkIcon className="size-[18px]" strokeWidth={2.25} />
        </span>
      );
    case "done":
      return (
        <span role="img" aria-label={label} className={cn(shape, "bg-success/15 text-success")}>
          <CheckIcon className="size-3" strokeWidth={3} />
        </span>
      );
    case "failed":
      return (
        <span
          role="img"
          aria-label={label}
          className={cn(shape, "bg-destructive/15 text-destructive")}
        >
          <XIcon className="size-3" strokeWidth={3} />
        </span>
      );
    case "stopped":
      return (
        <span
          role="img"
          aria-label={label}
          className={cn(shape, "border border-dashed border-status-gray")}
        >
          <span className="size-1.5 rounded-full bg-status-gray" />
        </span>
      );
    default:
      return (
        <span
          role="img"
          aria-label={label}
          className={cn(
            shape,
            "border text-[10px] font-medium tabular-nums",
            state === "ready"
              ? "border-session-active/60 bg-session-active/[0.08] text-session-active"
              : "border-border-strong text-muted-foreground",
          )}
        >
          {number}
        </span>
      );
  }
}

function stepDetail(item: StepProgress): ReactNode {
  const text = item.status ? textOf(item.status) : null;
  switch (item.state) {
    case "waiting":
      return item.step.goal;
    case "ready":
      return (
        <>
          <span className="font-medium text-session-active">Ready</span>
          {item.step.goal && ` · ${item.step.goal}`}
        </>
      );
    case "needs_input":
      // A running task becomes this while a prompt waits; its start has no text.
      return (
        <>
          <span className="font-medium text-brand-accent">{WAITING_FOR_YOU}</span>
          {` · ${text ?? "Respond in the chat"}`}
        </>
      );
    case "stopped":
      if (!item.status) return item.activeForm;
      return [stepStatusLabel("started", item.status.created_at, false), text]
        .filter(Boolean)
        .join(" · ");
    default:
      return text ?? item.activeForm;
  }
}

/** A running task's time so far, or when a finished one ended, stamped like a chat message. */
function StepMeta({ item }: { item: StepProgress }) {
  const { startedAt, status } = item;
  if (!status) return null;
  if (item.state === "done" || item.state === "failed") {
    const endedAt = status.created_at;
    return (
      <time
        dateTime={new Date(endedAt * 1000).toISOString()}
        title={
          startedAt === null
            ? absoluteTime(endedAt * 1000)
            : `${clock(startedAt)} – ${clock(endedAt)}`
        }
      >
        {formatMessageTimestamp(endedAt)}
      </time>
    );
  }
  if (
    startedAt !== null &&
    (item.state === "running" || (item.state === "needs_input" && statusOf(status) === "started"))
  ) {
    return (
      <span
        className={item.state === "running" ? "text-session-active" : undefined}
        title={`Started ${absoluteTime(startedAt * 1000)}`}
      >
        <Elapsed since={startedAt} />
      </span>
    );
  }
  return null;
}

const ENTRY_LABELS: Record<StepStatus, string> = {
  started: "Started",
  done: "Done",
  needs_input: WAITING_FOR_YOU,
  failed: "Failed",
};

const ENTRY_DOT_COLORS: Record<StepStatus, string> = {
  started: "bg-session-active",
  done: "bg-success",
  needs_input: "bg-brand-accent",
  failed: "bg-destructive",
};

/** "A", "A and B", "A, B and C". */
function listNames(names: readonly string[]): string {
  return names.length < 2
    ? (names[0] ?? "")
    : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/**
 * Each status a task recorded, oldest first, after what it waits on and its
 * goal. A line marks where each later attempt begins.
 */
function StepHistory({
  step,
  entries,
  names,
  showGoal,
}: {
  step: JournalPlanStep;
  entries: readonly JournalEntry[];
  names: ReadonlyMap<string, string>;
  /** False when the step's row already shows its goal. */
  showGoal: boolean;
}) {
  const history = entries.flatMap((entry) => {
    const status = entry.step === step.id && entry.kind === "status" ? statusOf(entry) : null;
    return status ? [{ entry, status }] : [];
  });
  const recorded = history.map(({ entry }) => entry);
  return (
    <div className="mt-1 mb-1.5 flex flex-col gap-2 rounded-lg bg-muted/70 px-3 py-2.5 text-sm">
      {step.depends_on.length > 0 && (
        <p className="break-words text-muted-foreground">
          After{" "}
          <span className="text-foreground">
            {listNames(step.depends_on.map((id) => names.get(id) ?? id))}
          </span>
        </p>
      )}
      {showGoal && step.goal && <p className="break-words text-muted-foreground">{step.goal}</p>}
      {history.length === 0 ? (
        <p className="text-muted-foreground">Nothing recorded yet.</p>
      ) : (
        <ol className="flex flex-col gap-1" aria-label={`${step.name} history`}>
          {history.map(({ entry, status }, index) => {
            const text = textOf(entry);
            return (
              <Fragment key={entry.id}>
                {isRestart(entry, recorded.slice(0, index)) && (
                  <li
                    className="flex items-center gap-2 py-0.5 text-xs text-muted-foreground"
                    data-testid="tasks-history-restart"
                  >
                    <span aria-hidden className="h-px flex-1 bg-border" />
                    Restarted
                    <span aria-hidden className="h-px flex-1 bg-border" />
                  </li>
                )}
                <li className="flex items-start gap-2">
                  <span
                    aria-hidden
                    className={cn(
                      "mt-[7px] size-1.5 shrink-0 rounded-full",
                      ENTRY_DOT_COLORS[status],
                    )}
                  />
                  <span className="min-w-0 flex-1 break-words text-muted-foreground">
                    <span className="text-foreground">{ENTRY_LABELS[status]}</span>
                    {text && ` · ${text}`}
                  </span>
                  <time
                    className="shrink-0 tabular-nums text-muted-foreground"
                    dateTime={new Date(entry.created_at * 1000).toISOString()}
                    title={absoluteTime(entry.created_at * 1000)}
                  >
                    {clock(entry.created_at)}
                  </time>
                </li>
              </Fragment>
            );
          })}
        </ol>
      )}
    </div>
  );
}

// What a task's ⋯ menu offers: Run while it's ready, Continue once it's stopped or
// while it waits for you, Retry once it failed, Stop while it runs.
type MenuAction = TaskAction | "stop";
const MENU_ACTION_LABELS: Record<MenuAction, string> = {
  run: "Run",
  continue: "Continue",
  proceed: "Continue",
  retry: "Retry",
  stop: "Stop",
};

const TASK_ACTION_FOR_STATE: Partial<Record<StepState, TaskAction>> = {
  ready: "run",
  stopped: "continue",
  needs_input: "proceed",
  failed: "retry",
};

export interface TaskMenuAction {
  action: MenuAction;
  onSelect: () => void;
}

/**
 * A task's menu action for its state, when the viewer can take it.
 *
 * @param onTaskAction Asks the agent to run, continue or retry the task; absent when the viewer can't send.
 * @param onStop Stops the agent; absent unless it works.
 */
export function taskMenuAction(
  item: StepProgress,
  onTaskAction: ((action: TaskAction, taskName: string) => void) | undefined,
  onStop: (() => void) | undefined,
): TaskMenuAction | undefined {
  if (item.state === "running") return onStop && { action: "stop", onSelect: onStop };
  const action = TASK_ACTION_FOR_STATE[item.state];
  if (action === undefined || !onTaskAction) return undefined;
  return { action, onSelect: () => onTaskAction(action, item.step.name) };
}

/** A task's ⋯ menu, on its row and on its timeline card: the action its state offers. */
function TaskMenu({
  taskName,
  menuAction,
  testIdPrefix,
  className,
  onOpenChange,
}: {
  taskName: string;
  menuAction: TaskMenuAction;
  /** Prefixes the button's and the item's test IDs, e.g. `tasks-row`. */
  testIdPrefix: string;
  className: string;
  /** Also told the menu closed when it goes while open, e.g. once its task has no action. */
  onOpenChange?: (open: boolean) => void;
}) {
  const openRef = useRef(false);
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  useEffect(
    () => () => {
      if (openRef.current) onOpenChangeRef.current?.(false);
    },
    [],
  );
  return (
    <DropdownMenu
      onOpenChange={(open) => {
        openRef.current = open;
        onOpenChange?.(open);
      }}
    >
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Actions for ${taskName}`}
          className={className}
          data-testid={`${testIdPrefix}-menu`}
        >
          <EllipsisIcon aria-hidden className="size-4" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem
          data-testid={`${testIdPrefix}-${menuAction.action}`}
          onSelect={() => menuAction.onSelect()}
        >
          {menuAction.action === "stop" ? (
            <SquareIcon className="size-3.5 fill-current" />
          ) : menuAction.action === "retry" ? (
            <RotateCcwIcon className="size-4" />
          ) : (
            <PlayIcon className="size-4" />
          )}
          {MENU_ACTION_LABELS[menuAction.action]}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// A task row's small icon buttons: its ⋯ menu and the chevron that opens its history.
const ROW_ICON_BUTTON =
  "mt-1.5 shrink-0 cursor-pointer rounded-md p-1 text-muted-foreground hover:bg-foreground/[0.06] hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50";

function StepRow({
  item,
  number,
  last,
  entries,
  names,
  flash,
  onShowInChat,
  menuAction,
}: {
  item: StepProgress;
  number: number;
  last: boolean;
  entries: readonly JournalEntry[];
  names: ReadonlyMap<string, string>;
  /** True for a moment after a click on its progress segment or its chat marker. */
  flash: boolean;
  /** Scroll the chat to a marker, e.g. ``journal-<entry id>``. */
  onShowInChat: (markerId: string) => void;
  /** What its ⋯ menu offers: Run, Continue, Retry or Stop; absent for none. */
  menuAction?: TaskMenuAction;
}) {
  const [open, setOpen] = useState(false);
  const detail = stepDetail(item);
  const waiting = item.state === "waiting";
  const emphasized =
    item.state === "running" || item.state === "needs_input" || item.state === "failed";
  // A click goes to the task's marker in the chat; one that hasn't started has none.
  const markerId = taskMarkerTarget(item);
  // The agent's own tasks have no history to open.
  const hasHistory = !item.native;
  const tint = flash
    ? "bg-session-active/12"
    : item.state === "running"
      ? "bg-session-active/[0.06]"
      : item.state === "needs_input"
        ? "bg-brand-accent/[0.06]"
        : undefined;
  const rowClassName = cn(
    "grid min-w-0 flex-1 grid-cols-[18px_minmax(0,1fr)_auto] items-start gap-x-3 rounded-lg px-2 py-1.5 text-left",
    markerId &&
      "cursor-pointer hover:bg-foreground/[0.04] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
  );
  const content = (
    <>
      <span className="pt-[3px]">
        <StepGlyph state={item.state} number={number} />
      </span>
      <span className="min-w-0">
        <span
          className={cn(
            "block break-words text-ui leading-6",
            waiting ? "text-muted-foreground" : "text-foreground",
            emphasized && "font-medium",
          )}
        >
          {item.step.name}
        </span>
        {detail && (
          <span className="mt-px line-clamp-3 block break-words text-pretty text-sm text-muted-foreground">
            {detail}
          </span>
        )}
      </span>
      <span className="pt-[3px] text-sm tabular-nums text-muted-foreground">
        <StepMeta item={item} />
      </span>
    </>
  );
  return (
    <li
      className="relative"
      data-testid="tasks-row"
      data-state={item.state}
      data-step-id={item.step.id}
      data-flash={flash || undefined}
    >
      {!last && (
        <span
          aria-hidden
          className="absolute top-[31px] -bottom-[5px] left-[16.5px] w-px bg-border"
        />
      )}
      <div className={cn("flex items-start rounded-lg transition-colors duration-500", tint)}>
        {markerId ? (
          <button
            type="button"
            onClick={() => onShowInChat(markerId)}
            className={rowClassName}
            title="Show in the chat"
            data-testid="tasks-row-show-in-chat"
          >
            {content}
          </button>
        ) : (
          <div className={rowClassName}>{content}</div>
        )}
        {menuAction && (
          <TaskMenu
            taskName={item.step.name}
            menuAction={menuAction}
            testIdPrefix="tasks-row"
            className={cn(ROW_ICON_BUTTON, !hasHistory && "mr-1")}
          />
        )}
        {hasHistory && (
          <button
            type="button"
            aria-expanded={open}
            aria-label={`${open ? "Hide" : "Show"} ${item.step.name} history`}
            onClick={() => setOpen((value) => !value)}
            className={cn(ROW_ICON_BUTTON, "mr-1")}
            data-testid="tasks-row-history"
          >
            <ChevronRightIcon
              aria-hidden
              className={cn("size-4 transition-transform", open && "rotate-90")}
            />
          </button>
        )}
      </div>
      {open && (
        <div className="pr-2 pl-[38px]">
          <StepHistory
            step={item.step}
            entries={entries}
            names={names}
            showGoal={item.state !== "waiting" && item.state !== "ready"}
          />
        </div>
      )}
    </li>
  );
}

const STATUS_LINE_COLORS: Partial<Record<StepState, string>> = {
  ready: "text-session-active",
  running: "text-session-active",
  failed: "text-destructive",
};

/** Where a task stands in a few words, e.g. "Done · 8:56 PM" or "Waiting on Run the tests". */
function statusLine(item: StepProgress, pending: readonly string[]): ReactNode {
  const { startedAt, status } = item;
  const ended = status ? formatMessageTimestamp(status.created_at) : null;
  switch (item.state) {
    case "waiting":
      return pending.length > 0 ? `Waiting on ${listNames(pending)}` : "Not started";
    case "ready":
      return "Ready to start";
    case "running":
      return startedAt === null ? (
        "Running"
      ) : (
        <>
          Running · <Elapsed since={startedAt} />
        </>
      );
    case "stopped":
      return status
        ? `Stopped · ${stepStatusLabel("started", status.created_at, false).toLowerCase()}`
        : "Stopped";
    case "done":
      return ended ? `Done · ${ended}` : "Done";
    case "failed":
      return ended ? `Failed · ${ended}` : "Failed";
    case "needs_input":
      return WAITING_FOR_YOU;
  }
}

/** Past this many tasks, the strip has no room for links between its segments. */
const MAX_LINKED_TASKS = 40;
/** How high a hover arc rises above the strip at most, in pixels. */
const ARC_HEIGHT = 14;

// The ⋯ button in the corner of a segment's card.
const CARD_MENU_BUTTON =
  "-my-1 -mr-1.5 shrink-0 cursor-pointer rounded-md p-1 text-muted-foreground hover:bg-foreground/[0.06] hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50";

/**
 * One segment per task; hovering one shows a card with which task it is, where
 * it stands and, given `menuActionFor`, its ⋯ menu. A plan links neighboring
 * segments when one task waits on the other, so a gap means the two can run at
 * the same time, and hovering one of its segments draws arcs to the tasks it
 * waits on and the tasks that wait on it.
 */
export function ProgressStrip({
  progress,
  fromPlan,
  onSelect,
  menuActionFor,
}: {
  progress: readonly StepProgress[];
  /** Whether the tasks are a plan's steps, the only tasks that say what they wait on. */
  fromPlan: boolean;
  onSelect: (stepId: string) => void;
  /** What a task's card offers in its ⋯ menu, as its row's menu does; none when absent. */
  menuActionFor?: (item: StepProgress) => TaskMenuAction | undefined;
}) {
  const names = new Map(progress.map((item) => [item.step.id, item.step.name]));
  const done = new Set(
    progress.filter((item) => item.state === "done").map((item) => item.step.id),
  );
  const links =
    fromPlan && progress.length <= MAX_LINKED_TASKS
      ? linkedToPrevious(progress.map((item) => item.step))
      : null;
  const roomy = progress.length <= 24;
  const gap = roomy ? 6 : 4;
  const [hovered, setHovered] = useState<number | null>(null);
  const focusIndex = links && hovered !== null && hovered < progress.length ? hovered : null;
  // The tasks the hovered one waits on, and the tasks that wait on it.
  const related = new Set<number>();
  if (focusIndex !== null) {
    const focusStep = progress[focusIndex].step;
    progress.forEach((item, index) => {
      if (
        focusStep.depends_on.includes(item.step.id) ||
        item.step.depends_on.includes(focusStep.id)
      ) {
        related.add(index);
      }
    });
  }
  const lit = (index: number) => focusIndex === null || index === focusIndex || related.has(index);
  // A segment's width, as CSS: the strip's width less its gaps, shared out.
  const unit = `((100% - ${(progress.length - 1) * gap}px) / ${progress.length})`;
  // The task whose card shows, one at a time, and the task whose card's ⋯ menu
  // is open, which keeps that card up while the pointer is in the menu.
  const [cardStep, setCardStep] = useState<string | null>(null);
  const [menuStep, setMenuStep] = useState<string | null>(null);
  return (
    <div
      className={cn(
        "relative flex h-4 items-center",
        // A plan's strip keeps room above it for the hover arcs.
        links ? "mt-px -mb-[5px]" : "-my-[5px]",
        !links && (progress.length > 16 ? "gap-px" : "gap-[3px]"),
      )}
      // In pixels, so the arcs' calc() lands on each segment's center.
      style={links ? { columnGap: gap } : undefined}
      data-testid="tasks-progress"
      data-links={links ? "true" : undefined}
      onPointerLeave={() => setHovered(null)}
    >
      {focusIndex !== null && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 bottom-[11px]"
          style={{ height: ARC_HEIGHT }}
        >
          {[...related].map((index) => {
            const from = Math.min(index, focusIndex);
            const span = Math.abs(index - focusIndex);
            return (
              <span
                key={index}
                className="absolute bottom-0 border-x-[1.5px] border-t-[1.5px] border-muted-foreground"
                style={{
                  left: `calc(${from + 0.5} * ${unit} + ${from * gap}px)`,
                  width: `calc(${span} * (${unit} + ${gap}px))`,
                  height: Math.min(4 + span * 2, ARC_HEIGHT),
                  borderTopLeftRadius: "50% 100%",
                  borderTopRightRadius: "50% 100%",
                }}
                data-testid="tasks-progress-arc"
              />
            );
          })}
        </div>
      )}
      {progress.map((item, index) => {
        const pending = item.step.depends_on
          .filter((id) => !done.has(id))
          .map((id) => names.get(id) ?? id);
        const text = item.status ? textOf(item.status) : null;
        const detail =
          item.state === "waiting" || item.state === "ready"
            ? item.step.goal
            : (text ?? item.step.goal);
        const menuAction = menuActionFor?.(item);
        const stepId = item.step.id;
        return (
          <HoverCard
            key={stepId}
            open={cardStep === stepId || menuStep === stepId}
            onOpenChange={(open) =>
              setCardStep((current) => (open ? stepId : current === stepId ? null : current))
            }
            // Once one card shows, moving along the strip shows the next at once.
            openDelay={cardStep === null ? 100 : 0}
            closeDelay={100}
          >
            <HoverCardTrigger asChild>
              <button
                type="button"
                aria-label={`Task ${index + 1}, ${item.step.name}: ${STATE_LABELS[item.state]}`}
                onClick={() => onSelect(item.step.id)}
                onPointerEnter={() => setHovered(index)}
                onFocus={() => setHovered(index)}
                onBlur={() => setHovered(null)}
                className="group relative flex h-full min-w-0 flex-1 cursor-pointer items-center focus-visible:outline-none"
                data-testid="tasks-progress-segment"
                data-linked={links?.[index] || undefined}
                data-dimmed={!lit(index) || undefined}
              >
                {links?.[index] && (
                  <span
                    aria-hidden
                    className={cn(
                      "absolute top-1/2 right-full h-0.5 -translate-y-1/2 bg-border-strong transition-opacity duration-150",
                      !(lit(index) && lit(index - 1)) && "opacity-30",
                    )}
                    style={{ width: gap }}
                  />
                )}
                <span
                  className={cn(
                    "h-1.5 w-full rounded-full transition-[height,opacity] duration-150 group-hover:h-2 group-focus-visible:h-2 group-focus-visible:ring-2 group-focus-visible:ring-ring/50",
                    SEGMENT_COLORS[item.state],
                    !lit(index) && "animate-none opacity-30",
                  )}
                />
              </button>
            </HoverCardTrigger>
            <HoverCardContent
              side="bottom"
              sideOffset={8}
              className="flex w-fit max-w-72 flex-col items-start gap-1 px-3 py-2 text-sm"
              data-testid="tasks-progress-card"
            >
              <span className="flex w-full items-center justify-between gap-3">
                <span className="text-muted-foreground">
                  Task {index + 1} of {progress.length}
                </span>
                {menuAction && (
                  <TaskMenu
                    taskName={item.step.name}
                    menuAction={menuAction}
                    testIdPrefix="tasks-progress"
                    className={CARD_MENU_BUTTON}
                    onOpenChange={(open) => {
                      setMenuStep(open ? stepId : null);
                      // Picking an action, or closing the menu, puts the card away too.
                      if (!open) setCardStep(null);
                    }}
                  />
                )}
              </span>
              <span className="break-words font-medium">{item.step.name}</span>
              <span className="flex items-center gap-1.5">
                <StepGlyph state={item.state} number={index + 1} />
                <span className={STATUS_LINE_COLORS[item.state]}>{statusLine(item, pending)}</span>
              </span>
              {detail && (
                <span className="line-clamp-3 break-words text-muted-foreground">{detail}</span>
              )}
            </HoverCardContent>
          </HoverCard>
        );
      })}
    </div>
  );
}

/**
 * Marks a plan that runs a saved workflow, or was saved as one, with the
 * workflow's name on hover. With the Workflows page on, it opens the
 * workflow's page.
 */
function WorkflowChip({ workflowId }: { workflowId: string }) {
  const { workflows } = useSavedWorkflows();
  const name = workflows.find((workflow) => workflow.id === workflowId)?.name;
  const linked = isFeatureEnabled(useServerInfo(), "workflows_page");
  const className = "h-5 gap-1 px-1.5 text-xs font-normal text-muted-foreground";
  const label = (
    <>
      <WorkflowIcon className="size-3" />
      Workflow
    </>
  );
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          {linked ? (
            <Badge asChild variant="outline" className={className}>
              <Link
                to={`/workflows/${encodeURIComponent(workflowId)}`}
                componentId="tasks.plan.workflow"
                data-testid="tasks-plan-workflow"
              >
                {label}
              </Link>
            </Badge>
          ) : (
            <Badge
              variant="outline"
              className={className}
              tabIndex={0}
              data-testid="tasks-plan-workflow"
            >
              {label}
            </Badge>
          )}
        </TooltipTrigger>
        <TooltipContent>
          {name ? `Saved workflow “${name}”` : "From a saved workflow"}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/**
 * The tasks' summary and progress strip. It stays at the top of the panel while
 * the rest scrolls, with a hairline under it once something scrolls beneath.
 */
function TasksHeader({
  plan,
  workflowId,
  progress,
  paused,
  stuck,
  onSelect,
  onSaveWorkflow,
  onContinuePlan,
  onTaskAction,
  onStop,
}: {
  plan: JournalEntry | null;
  /** The saved workflow the plan came from, or null. */
  workflowId: string | null;
  progress: readonly StepProgress[];
  paused: PausedLabel | null;
  stuck: boolean;
  onSelect: (stepId: string) => void;
  /** Opens the composer's Save as workflow dialog; absent when the agent can't save. */
  onSaveWorkflow?: () => void;
  /** Asks the agent to continue the plan; absent when this viewer can't send. */
  onContinuePlan?: () => void;
  /** The composer's Run, Continue or Retry for a task's card; absent when this viewer can't send. */
  onTaskAction?: (action: TaskAction, taskName: string) => void;
  /** Stops the agent; offered only while it works. */
  onStop?: () => void;
}) {
  const tally = planTally(progress);
  const updatedAt = Math.max(
    plan?.created_at ?? 0,
    ...progress.map((item) => item.status?.created_at ?? 0),
  );
  const extras: { state: StepState; className: string }[] = [
    { state: "needs_input" as const, className: "text-brand-accent" },
    { state: "running" as const, className: "text-session-active" },
    { state: "failed" as const, className: "text-destructive" },
  ].filter((extra) => tally[extra.state] > 0);
  const finished = finishedTaskCount(progress);
  const allDone = tally.done === progress.length;
  const name = planName(plan);
  // Save opens the composer's dialog once this menu has closed, so the dialog keeps focus.
  const saveAfterCloseRef = useRef(false);
  return (
    <header
      className={cn(
        "tasks-header sticky top-0 z-10 -mx-2 -mb-7 flex flex-col gap-2.5 px-4 pt-4 pb-3 transition-shadow",
        stuck && "shadow-[0_1px_0_var(--border)]",
      )}
      data-testid="tasks-header"
    >
      <div className="flex flex-col gap-0.5">
        {/* The tab already says Tasks, so the plan's name is the heading. */}
        {(name || updatedAt > 0) && (
          <div className="flex items-baseline justify-between gap-3">
            {name && (
              // The chip follows the name, and wraps under it when the panel is narrow.
              <div className="flex min-w-0 flex-wrap items-baseline gap-x-1.5 gap-y-1">
                <h3
                  className="line-clamp-2 min-w-0 break-words text-ui font-semibold leading-snug"
                  data-testid="tasks-plan-name"
                >
                  {name}
                </h3>
                {workflowId && <WorkflowChip workflowId={workflowId} />}
              </div>
            )}
            <span className="ml-auto flex shrink-0 items-center gap-1">
              {updatedAt > 0 && (
                <span className="text-sm text-muted-foreground">
                  Updated <Ago seconds={updatedAt} />
                </span>
              )}
              {onStop ? (
                <PlanRunButton
                  control="stop"
                  size="icon-xs"
                  className="self-center"
                  onClick={onStop}
                />
              ) : (
                onContinuePlan &&
                paused && (
                  <PlanRunButton
                    control="play"
                    size="icon-xs"
                    className="self-center"
                    onClick={onContinuePlan}
                  />
                )
              )}
              {onSaveWorkflow && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      aria-label="Plan actions"
                      className="shrink-0 self-center text-muted-foreground"
                      data-testid="tasks-plan-menu"
                    >
                      <EllipsisIcon className="size-3.5" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent
                    align="end"
                    onCloseAutoFocus={(event) => {
                      if (!saveAfterCloseRef.current) return;
                      saveAfterCloseRef.current = false;
                      event.preventDefault();
                      requestAnimationFrame(() => onSaveWorkflow());
                    }}
                  >
                    <DropdownMenuItem
                      data-testid="tasks-save-workflow"
                      onSelect={() => {
                        saveAfterCloseRef.current = true;
                      }}
                    >
                      <WorkflowIcon className="size-3.5" />
                      Save as workflow…
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </span>
          </div>
        )}
        {!name && <h3 className="sr-only">Tasks</h3>}
        <p className="flex flex-wrap items-baseline gap-x-1.5 text-ui leading-6">
          <span className="font-medium" data-testid="tasks-summary">
            {allDone
              ? `All ${progress.length} tasks done`
              : finished === progress.length
                ? `All ${progress.length} tasks finished`
                : `${finished} of ${progress.length} finished`}
          </span>
          {extras.map((extra) => (
            <span key={extra.state} className="text-sm text-muted-foreground">
              ·{" "}
              <span className={cn("font-medium", extra.className)}>
                {tally[extra.state]} {STATE_LABELS[extra.state].toLowerCase()}
              </span>
            </span>
          ))}
          {paused && (
            <span className="text-sm text-muted-foreground" data-testid="tasks-paused">
              · <span className="font-medium text-foreground">{paused}</span>
            </span>
          )}
        </p>
      </div>
      <ProgressStrip
        progress={progress}
        fromPlan={plan !== null}
        onSelect={onSelect}
        menuActionFor={(item) => taskMenuAction(item, onTaskAction, onStop)}
      />
    </header>
  );
}

function TaskList({
  progress,
  entries,
  names,
  flashStep,
  onShowInChat,
  onTaskAction,
  onStop,
}: {
  progress: readonly StepProgress[];
  entries: readonly JournalEntry[];
  names: ReadonlyMap<string, string>;
  flashStep: string | null;
  onShowInChat: (markerId: string) => void;
  /** The composer's Run, Continue or Retry, given a task's name; absent when this viewer can't send. */
  onTaskAction?: (action: TaskAction, taskName: string) => void;
  /** The composer's Stop; present only while the agent works. */
  onStop?: () => void;
}) {
  return (
    <section aria-label="Tasks">
      <ol className="flex flex-col">
        {progress.map((item, index) => (
          <StepRow
            key={item.step.id}
            item={item}
            number={index + 1}
            last={index === progress.length - 1}
            entries={entries}
            names={names}
            flash={flashStep === item.step.id}
            onShowInChat={onShowInChat}
            menuAction={taskMenuAction(item, onTaskAction, onStop)}
          />
        ))}
      </ol>
    </section>
  );
}

/**
 * The session's Tasks tab: where its tasks stand, one row each. The agent's
 * notes aren't shown.
 *
 * @param onStartWorkflow - Runs before the empty state's "run a workflow" link
 *   starts one, e.g. to close the drawer it sits in on a phone.
 */
export function TasksPanel({
  conversationId,
  active,
  onStartWorkflow,
  onShowInChat,
}: {
  conversationId: string;
  active: boolean;
  onStartWorkflow?: () => void;
  /** Called before a task's click scrolls the chat to it, e.g. to close a phone's drawer. */
  onShowInChat?: () => void;
}) {
  const tasks = useSessionTasks(conversationId, active);
  const { journal, plan, progress } = tasks;
  const startWorkflow = useSessionTasksActions((state) => state.startWorkflow[conversationId]);
  const saveWorkflow = useSessionTasksActions((state) => state.saveWorkflow[conversationId]);
  const taskAction = useSessionTasksActions((state) => state.taskAction[conversationId]);
  const stopWork = useSessionTasksActions((state) => state.stopWork[conversationId]);
  const continuePlan = useSessionTasksActions((state) => state.continuePlan[conversationId]);
  const entries = tasks.journalSupported ? journal.entries : NO_ENTRIES;
  const names = new Map(progress.map((item) => [item.step.id, item.step.name]));
  const empty = progress.length === 0;
  const showPlan = !tasks.loading && !journal.error && progress.length > 0;

  const scrollRef = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(false);
  const [flashStep, setFlashStep] = useState<string | null>(null);
  const jumpToStep = useCallback((stepId: string) => {
    const rows = scrollRef.current?.querySelectorAll<HTMLElement>("[data-step-id]") ?? [];
    const row = [...rows].find((element) => element.dataset.stepId === stepId);
    if (!row) return;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    row.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
    setFlashStep(stepId);
  }, []);
  useEffect(() => {
    if (!flashStep) return;
    const timer = window.setTimeout(() => setFlashStep(null), 1400);
    return () => window.clearTimeout(timer);
  }, [flashStep]);
  // A click on a task's chat marker asks for its row once the tab shows.
  const taskToShow = useSessionTasksActions((state) => state.taskToShow);
  useEffect(() => {
    if (!active || !showPlan || taskToShow?.conversationId !== conversationId) return;
    useSessionTasksActions.setState({ taskToShow: null });
    // The rail or drawer may still be opening; its rows mount by the next frame.
    requestAnimationFrame(() => jumpToStep(taskToShow.stepId));
  }, [active, showPlan, taskToShow, conversationId, jumpToStep]);
  const [, setSearchParams] = useSearchParams();
  const showInChat = useCallback(
    (markerId: string) => {
      onShowInChat?.();
      setSearchParams((current) => messageDeepLinkParams(current, markerId), { replace: true });
    },
    [onShowInChat, setSearchParams],
  );

  return (
    <div
      ref={scrollRef}
      className="flex min-h-0 flex-1 flex-col overflow-y-auto"
      data-testid="tasks-panel"
      onScroll={(event) => setStuck(event.currentTarget.scrollTop > 0)}
    >
      <div className={cn("flex flex-1 flex-col gap-7 px-2 pb-4", !showPlan && "pt-4")}>
        {tasks.loading ? (
          <div role="status" aria-label="Loading tasks" className="flex flex-col gap-4 px-4 pt-1">
            <span className="h-3 w-24 animate-pulse rounded-full bg-muted motion-reduce:animate-none" />
            {[64, 80, 52].map((width) => (
              <span key={width} className="flex items-start gap-3">
                <span className="size-[18px] shrink-0 animate-pulse rounded-full bg-muted motion-reduce:animate-none" />
                <span className="flex flex-1 flex-col gap-1.5 pt-0.5">
                  <span
                    className="h-3 animate-pulse rounded-full bg-muted motion-reduce:animate-none"
                    style={{ width: `${width}%` }}
                  />
                  <span className="h-2.5 w-2/5 animate-pulse rounded-full bg-muted/70 motion-reduce:animate-none" />
                </span>
              </span>
            ))}
          </div>
        ) : journal.error ? (
          <p className="px-2 py-6 text-center text-sm text-muted-foreground">
            {journal.error.message}
          </p>
        ) : empty ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-1.5 px-6 text-center">
            <ListChecksIcon aria-hidden className="mb-1.5 size-6 text-muted-foreground" />
            <p className="text-ui font-medium">No tasks yet</p>
            <p className="max-w-64 text-sm text-muted-foreground">
              Tasks show up here when the agent makes a to-do list
              {startWorkflow && (
                <>
                  {" "}
                  or you{" "}
                  <button
                    type="button"
                    className="cursor-pointer font-medium text-link hover:underline focus-visible:underline focus-visible:outline-none"
                    onClick={() => {
                      onStartWorkflow?.();
                      startWorkflow();
                    }}
                  >
                    run a workflow
                  </button>
                </>
              )}
              .
            </p>
          </div>
        ) : (
          <>
            <TasksHeader
              plan={plan}
              workflowId={planWorkflowId(entries)}
              progress={progress}
              paused={tasks.paused}
              stuck={stuck}
              onSelect={jumpToStep}
              onSaveWorkflow={
                saveWorkflow && plan ? () => saveWorkflow(planName(plan) ?? "") : undefined
              }
              onContinuePlan={continuePlan && (() => continuePlan(planName(plan)))}
              onTaskAction={taskAction}
              onStop={stopWork}
            />
            <TaskList
              progress={progress}
              entries={entries}
              names={names}
              flashStep={flashStep}
              onShowInChat={showInChat}
              onTaskAction={taskAction}
              onStop={stopWork}
            />
          </>
        )}
      </div>
    </div>
  );
}
