// The Tasks tab: where the session's tasks stand, one row each, then the
// agent's notes. Tasks are a plan's steps, or the agent's own task list when
// the session has no plan.

import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  CheckIcon,
  ListChecksIcon,
  Loader2Icon,
  MessageCircleQuestionMarkIcon,
  XIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useNow } from "@/hooks/useNow";
import { useSessionTasks } from "@/hooks/useSessionTasks";
import { absoluteTime, formatDuration, relativeTime } from "@/lib/relativeTime";
import {
  finishedTaskCount,
  isRestart,
  linkedToPrevious,
  noteBody,
  planName,
  planTally,
  sessionNotes,
  statusOf,
  stepStatusLabel,
  textOf,
  WAITING_FOR_YOU,
  type JournalEntry,
  type JournalPlanStep,
  type PausedLabel,
  type StepProgress,
  type StepState,
} from "@/lib/sessionJournal";
import { cn } from "@/lib/utils";
import { useSessionTasksActions } from "@/store/sessionTasksActions";

const NO_ENTRIES: JournalEntry[] = [];

const SECTION_HEADING = "text-xs font-medium tracking-wide text-muted-foreground uppercase";

const STATE_LABELS: Record<StepState, string> = {
  waiting: "Not started",
  ready: "Ready",
  running: "Running",
  stopped: "Stopped",
  needs_input: WAITING_FOR_YOU,
  done: "Done",
  failed: "Failed",
};

const SEGMENT_COLORS: Record<StepState, string> = {
  waiting: "bg-foreground/10",
  ready: "bg-session-active/25",
  running: "bg-session-active",
  stopped: "bg-status-gray",
  needs_input: "bg-brand-accent",
  done: "bg-success",
  failed: "bg-destructive",
};

// Notes are short Markdown; style it at the panel's own sizes.
const NOTE_MARKDOWN = cn(
  "break-words [&_a]:text-link [&_a]:underline [&_code]:rounded [&_code]:bg-muted",
  "[&_code]:px-1 [&_code]:font-mono [&_code]:text-[0.92em] [&_h1,&_h2,&_h3,&_h4]:font-medium",
  "[&_h1,&_h2,&_h3,&_h4]:text-foreground [&_li]:my-0.5 [&_ol]:list-decimal [&_ol]:pl-5",
  "[&_p+p]:mt-1.5 [&_strong]:font-medium [&_strong]:text-foreground [&_table]:my-1",
  "[&_td]:pr-3 [&_th]:pr-3 [&_th]:text-left [&_ul]:list-disc [&_ul]:pl-5",
);

function clock(seconds: number): string {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function firstLine(markdown: string): string {
  return (markdown.trim().split("\n")[0] ?? "").replace(/^#+\s*/, "");
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
function StepGlyph({ state, number }: { state: StepState; number?: number }) {
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

function StepMeta({ item }: { item: StepProgress }) {
  const { startedAt, status } = item;
  if (startedAt === null || !status) return null;
  if (
    item.state === "running" ||
    (item.state === "needs_input" && statusOf(status) === "started")
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
  if (item.state === "done" || item.state === "failed") {
    return (
      <span title={`${clock(startedAt)} – ${clock(status.created_at)}`}>
        {formatDuration(status.created_at - startedAt)}
      </span>
    );
  }
  return null;
}

/** A small dot for one recorded status or note, in the status's color. */
function EntryDot({ entry, className }: { entry: JournalEntry; className?: string }) {
  const status = statusOf(entry);
  const color =
    status === "done"
      ? "bg-success"
      : status === "failed"
        ? "bg-destructive"
        : status === "needs_input"
          ? "bg-brand-accent"
          : status === "started"
            ? "bg-session-active"
            : "bg-foreground-tertiary";
  return <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", color, className)} />;
}

function entryLabel(entry: JournalEntry): string {
  const status = statusOf(entry);
  if (!status) return "Note";
  return {
    started: "Started",
    done: "Done",
    needs_input: WAITING_FOR_YOU,
    failed: "Failed",
  }[status];
}

function entryText(entry: JournalEntry): string | null {
  return entry.kind === "note" ? firstLine(noteBody(entry)) || null : textOf(entry);
}

/** "A", "A and B", "A, B and C". */
function listNames(names: readonly string[]): string {
  return names.length < 2
    ? (names[0] ?? "")
    : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/**
 * Everything recorded for one task, oldest first, after what it waits on and its
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
  const history = entries.filter(
    (entry) => entry.step === step.id && (entry.kind === "status" || entry.kind === "note"),
  );
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
          {history.map((entry, index) => {
            const text = entryText(entry);
            return (
              <Fragment key={entry.id}>
                {isRestart(entry, history.slice(0, index)) && (
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
                  <EntryDot entry={entry} className="mt-[7px]" />
                  <span className="min-w-0 flex-1 break-words text-muted-foreground">
                    <span className="text-foreground">{entryLabel(entry)}</span>
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

/** A note's title (its first line, when that line heads the rest) and body. */
function splitNote(markdown: string): { title: string | null; body: string } {
  const [first, ...rest] = markdown.trim().split("\n");
  const body = rest.join("\n").trim();
  if (/^#+\s/.test(first) || (body && first.length <= 100)) {
    return { title: first.replace(/^#+\s*/, ""), body };
  }
  return { title: null, body: markdown.trim() };
}

/** A note the agent wrote: a title line, then Markdown shortened until opened when long. */
function NoteBlock({ entry, className }: { entry: JournalEntry; className?: string }) {
  const { title, body } = splitNote(noteBody(entry));
  const [open, setOpen] = useState(false);
  const long = body.length > 160 || body.split("\n").length > 2;
  return (
    <div
      className={cn("rounded-lg bg-muted/70 px-3 py-2 text-sm text-muted-foreground", className)}
      data-testid="tasks-note"
    >
      {title && <p className="break-words font-medium text-foreground">{title}</p>}
      {body && (
        <div className={cn(NOTE_MARKDOWN, title && "mt-0.5", long && !open && "line-clamp-2")}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
            {body}
          </ReactMarkdown>
        </div>
      )}
      {long && (
        <button
          type="button"
          className="mt-1 cursor-pointer font-medium text-muted-foreground hover:text-foreground"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

function StepRow({
  item,
  number,
  last,
  entries,
  names,
  flash,
}: {
  item: StepProgress;
  number: number;
  last: boolean;
  entries: readonly JournalEntry[];
  names: ReadonlyMap<string, string>;
  /** True for a moment after a click on its progress segment. */
  flash: boolean;
}) {
  const [open, setOpen] = useState(false);
  const detail = stepDetail(item);
  const waiting = item.state === "waiting";
  const emphasized =
    item.state === "running" || item.state === "needs_input" || item.state === "failed";
  // The agent's own tasks have no history to open.
  const expandable = !item.native;
  const rowClassName = cn(
    "grid w-full grid-cols-[18px_minmax(0,1fr)_auto] items-start gap-x-3 rounded-lg px-2 py-1.5 text-left transition-colors duration-500",
    expandable &&
      "cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
    flash
      ? "bg-session-active/12"
      : item.state === "running"
        ? cn("bg-session-active/[0.06]", expandable && "hover:bg-session-active/10")
        : item.state === "needs_input"
          ? cn("bg-brand-accent/[0.06]", expandable && "hover:bg-brand-accent/10")
          : expandable && "hover:bg-muted/50",
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
      {expandable ? (
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className={rowClassName}
        >
          {content}
        </button>
      ) : (
        <div className={rowClassName}>{content}</div>
      )}
      {(item.note || open) && (
        <div className="pr-2 pl-[38px]">
          {open ? (
            <StepHistory
              step={item.step}
              entries={entries}
              names={names}
              showGoal={item.state !== "waiting" && item.state !== "ready"}
            />
          ) : (
            item.note && <NoteBlock entry={item.note} className="mt-1 mb-1.5" />
          )}
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

/** Where a task stands in a few words, e.g. "Done in 14s" or "Waiting on Run the tests". */
function statusLine(item: StepProgress, pending: readonly string[]): ReactNode {
  const { startedAt, status } = item;
  const took = startedAt !== null && status ? formatDuration(status.created_at - startedAt) : null;
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
      return took ? `Done in ${took}` : "Done";
    case "failed":
      return took ? `Failed after ${took}` : "Failed";
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
function ProgressStrip({
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
                      !lit(index) && "opacity-30",
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
 * The tasks' summary and progress strip. It stays at the top of the panel while
 * the rest scrolls, with a hairline under it once something scrolls beneath.
 */
function TasksHeader({
  plan,
  progress,
  paused,
  stuck,
  onSelect,
}: {
  plan: JournalEntry | null;
  progress: readonly StepProgress[];
  paused: PausedLabel | null;
  stuck: boolean;
  onSelect: (stepId: string) => void;
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
              <h3
                className="line-clamp-2 min-w-0 break-words text-ui font-semibold leading-snug"
                data-testid="tasks-plan-name"
              >
                {name}
              </h3>
            )}
            {updatedAt > 0 && (
              <span className="ml-auto shrink-0 text-sm text-muted-foreground">
                Updated <Ago seconds={updatedAt} />
              </span>
            )}
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
}: {
  progress: readonly StepProgress[];
  entries: readonly JournalEntry[];
  names: ReadonlyMap<string, string>;
  flashStep: string | null;
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
          />
        ))}
      </ol>
    </section>
  );
}

function NotesSection({ notes }: { notes: readonly JournalEntry[] }) {
  return (
    <section aria-label="Notes" className="flex flex-col gap-2 px-2">
      <h3 className={SECTION_HEADING}>Notes</h3>
      <ol className="flex flex-col gap-2">
        {notes.map((note) => (
          <li key={note.id} className="flex flex-col gap-1">
            <NoteBlock entry={note} />
            <span className="px-1 text-sm text-muted-foreground">
              <Ago seconds={note.created_at} />
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * The session's Tasks tab: where its tasks stand, one row each, and the
 * agent's notes.
 *
 * @param onStartWorkflow - Runs before the empty state's "run a workflow" link
 *   starts one, e.g. to close the drawer it sits in on a phone.
 */
export function TasksPanel({
  conversationId,
  active,
  onStartWorkflow,
}: {
  conversationId: string;
  active: boolean;
  onStartWorkflow?: () => void;
}) {
  const tasks = useSessionTasks(conversationId, active);
  const { journal, plan, progress } = tasks;
  const startWorkflow = useSessionTasksActions((state) => state.startWorkflow[conversationId]);
  const entries = tasks.journalSupported ? journal.entries : NO_ENTRIES;
  const notes = sessionNotes(entries);
  const names = new Map(progress.map((item) => [item.step.id, item.step.name]));
  const empty = progress.length === 0 && notes.length === 0;
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
            {showPlan && (
              <>
                <TasksHeader
                  plan={plan}
                  progress={progress}
                  paused={tasks.paused}
                  stuck={stuck}
                  onSelect={jumpToStep}
                />
                <TaskList
                  progress={progress}
                  entries={entries}
                  names={names}
                  flashStep={flashStep}
                />
              </>
            )}
            {notes.length > 0 && <NotesSection notes={notes} />}
            {journal.hasOlder && (
              <div className="flex justify-center">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={journal.loadingOlder}
                  onClick={journal.loadOlder}
                >
                  {journal.loadingOlder ? "Loading…" : "Show older notes"}
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
