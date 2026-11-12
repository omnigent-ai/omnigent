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
  onContinue,
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
  /** Asks the agent to pick the task back up; offered while it's stopped. */
  onContinue?: () => void;
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
        {onContinue && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                aria-label={`Actions for ${item.step.name}`}
                className={cn(ROW_ICON_BUTTON, !hasHistory && "mr-1")}
                data-testid="tasks-row-menu"
              >
                <EllipsisIcon aria-hidden className="size-4" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem data-testid="tasks-row-continue" onSelect={onContinue}>
                <PlayIcon className="size-4" />
                Continue
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
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

/**
 * One segment per task; hovering one says which task it is and where it stands.
 * A plan links neighboring segments when one task waits on the other, so a gap
 * means the two can run at the same time, and hovering one of its segments
 * draws arcs to the tasks it waits on and the tasks that wait on it.
 */
export function ProgressStrip({
  progress,
  fromPlan,
  onSelect,
}: {
  progress: readonly StepProgress[];
  /** Whether the tasks are a plan's steps, the only tasks that say what they wait on. */
  fromPlan: boolean;
  onSelect: (stepId: string) => void;
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
  return (
    <TooltipProvider delayDuration={100} skipDelayDuration={400} disableHoverableContent>
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
          return (
            <Tooltip key={item.step.id}>
              <TooltipTrigger asChild>
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
              </TooltipTrigger>
              <TooltipContent
                side="bottom"
                className="max-w-72 flex-col items-start gap-1 px-3 py-2"
              >
                <span className="text-muted-foreground">
                  Task {index + 1} of {progress.length}
                </span>
                <span className="break-words font-medium">{item.step.name}</span>
                <span className="flex items-center gap-1.5">
                  <StepGlyph state={item.state} number={index + 1} />
                  <span className={STATUS_LINE_COLORS[item.state]}>
                    {statusLine(item, pending)}
                  </span>
                </span>
                {detail && (
                  <span className="line-clamp-3 break-words text-muted-foreground">{detail}</span>
                )}
              </TooltipContent>
            </Tooltip>
          );
        })}
      </div>
    </TooltipProvider>
  );
}

/**
 * Marks a plan that runs a saved workflow, or was saved as one, with the
 * workflow's name on hover. It opens the Workflows page when there is one.
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
                to="/workflows"
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
  progress,
  paused,
  stuck,
  onSelect,
  onSaveWorkflow,
}: {
  plan: JournalEntry | null;
  progress: readonly StepProgress[];
  paused: PausedLabel | null;
  stuck: boolean;
  onSelect: (stepId: string) => void;
  /** Opens the composer's Save as workflow dialog; absent when the agent can't save. */
  onSaveWorkflow?: () => void;
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
                {plan?.workflow_id && <WorkflowChip workflowId={plan.workflow_id} />}
              </div>
            )}
            <span className="ml-auto flex shrink-0 items-center gap-1">
              {updatedAt > 0 && (
                <span className="text-sm text-muted-foreground">
                  Updated <Ago seconds={updatedAt} />
                </span>
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
      <ProgressStrip progress={progress} fromPlan={plan !== null} onSelect={onSelect} />
    </header>
  );
}

function TaskList({
  progress,
  entries,
  names,
  flashStep,
  onShowInChat,
  onContinueTask,
}: {
  progress: readonly StepProgress[];
  entries: readonly JournalEntry[];
  names: ReadonlyMap<string, string>;
  flashStep: string | null;
  onShowInChat: (markerId: string) => void;
  /** The composer's Continue, given a task's name; absent when this viewer can't send. */
  onContinueTask?: (taskName: string) => void;
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
            onContinue={
              onContinueTask && item.state === "stopped"
                ? () => onContinueTask(item.step.name)
                : undefined
            }
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
  const continueTask = useSessionTasksActions((state) => state.continueTask[conversationId]);
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
              progress={progress}
              paused={tasks.paused}
              stuck={stuck}
              onSelect={jumpToStep}
              onSaveWorkflow={
                saveWorkflow && plan ? () => saveWorkflow(planName(plan) ?? "") : undefined
              }
            />
            <TaskList
              progress={progress}
              entries={entries}
              names={names}
              flashStep={flashStep}
              onShowInChat={showInChat}
              onContinueTask={continueTask}
            />
          </>
        )}
      </div>
    </div>
  );
}
