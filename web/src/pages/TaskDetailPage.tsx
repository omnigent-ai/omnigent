/**
 * Scheduled-task DETAIL page (`/tasks/:taskId`) — opened by clicking a task
 * card on the `/tasks` list. Shows the task's header (title + edit / delete /
 * Run now), a status line (pause-resume toggle + Active/Paused pill + the
 * human-readable schedule + relative next-run), the Prompt, a Configuration
 * block (agent + host), and the run history.
 *
 * All data + mutations come from the existing scheduled-tasks hooks; editing
 * reuses `CreateScheduledTaskDialog` in its edit mode. Nothing here is
 * fabricated: each run renders only the fields the server actually returns
 * (status, timestamp, duration, an errorCode-derived message, and an
 * open-conversation link) — never an invented summary line.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  CalendarOffIcon,
  ChevronLeftIcon,
  Loader2Icon,
  PencilIcon,
  PlayIcon,
  Trash2Icon,
  TriangleAlertIcon,
} from "lucide-react";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { showToast } from "@/components/ui/toast";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import {
  cancelRunNowPoll,
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTask,
  useScheduledTaskRuns,
  useUpdateScheduledTask,
} from "@/hooks/useScheduledTasks";
import {
  isConversationUnseen,
  isExplicitlyUnread,
  seedRunUnreadBaseline,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { useNow } from "@/hooks/useNow";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import {
  describeRunError,
  describeSchedule,
  formatNextRunAt,
  formatRunDuration,
  formatRunTimestamp,
} from "@/lib/scheduleText";
import { ScheduledTaskApiError } from "@/lib/scheduledTasksApi";
import type { ScheduledTaskRun } from "@/lib/scheduledTasksApi";
import { cn } from "@/lib/utils";

export function TaskDetailPage() {
  const { taskId = "" } = useParams<{ taskId: string }>();
  const navigate = useNavigate();
  const now = useNow();

  const { data: task, isLoading, isError } = useScheduledTask(taskId);
  // Run history only fetches once we have a real id.
  const {
    data: runs,
    isLoading: runsLoading,
    isError: runsError,
  } = useScheduledTaskRuns(taskId, taskId !== "");

  const updateMutation = useUpdateScheduledTask();
  const deleteMutation = useDeleteScheduledTask();
  const runNowMutation = useRunScheduledTaskNow();

  const [editOpen, setEditOpen] = useState(false);

  // "Awaiting new run row" loading state for the Run now button. The button
  // stays in its loading state after the POST returns 202 until the accelerated
  // poll delivers a new run row (newest id changes) or the 20s safety cap fires.
  const [awaitingRunRow, setAwaitingRunRow] = useState(false);
  const preFireNewestIdRef = useRef<string | null>(null);
  const awaitingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Clear the awaiting flag and its safety timer (shared by the effect and
  // the timer callback so neither leaks).
  function clearAwaitingRunRow() {
    setAwaitingRunRow(false);
    if (awaitingTimerRef.current != null) {
      clearTimeout(awaitingTimerRef.current);
      awaitingTimerRef.current = null;
    }
  }

  // Clear the flag when a new run row appears (newestId changed from snapshot).
  useEffect(() => {
    if (!awaitingRunRow) return;
    const newestId = runs?.[0]?.id ?? null;
    if (newestId != null && newestId !== preFireNewestIdRef.current) {
      clearAwaitingRunRow();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs, awaitingRunRow]);

  // Unmount cleanup so neither the local safety timer nor the module-global
  // run-now poller fires into / refetches for an unmounted page.
  useEffect(() => {
    return () => {
      if (awaitingTimerRef.current != null) clearTimeout(awaitingTimerRef.current);
      // Stop the accelerated run-now poll this page kicked off (self-scheduling
      // setTimeout chain in useRunScheduledTaskNow) once we navigate away.
      if (taskId !== "") cancelRunNowPoll(taskId);
    };
  }, [taskId]);

  // Auto-mark newly-completed (succeeded) runs as unread so the pink dot
  // appears until the user opens the conversation.
  //
  // Two guards prevent incorrect behaviour:
  //   1. ONCE-PER-SESSION: `autoMarkedRef` tracks every conversationId this
  //      component has already auto-marked. Re-marking on a subsequent poll
  //      would resurrect the dot for a run the user already opened.
  //   2. NO-RETROACTIVE: the ref is seeded on the FIRST non-empty runs response
  //      with all currently-terminal ids, so pre-existing history is never
  //      mass-marked pink on page load. Only runs that appear as terminal AFTER
  //      the baseline is established trigger a mark.
  const autoMarkedRef = useRef<Set<string>>(new Set());
  const baselineSeededRef = useRef(false);
  useEffect(() => {
    if (!runs) return;

    if (!baselineSeededRef.current) {
      // Seed the baseline from the initial run list — skip all currently-terminal
      // ids so existing history stays read. Seed even when empty so a subsequent
      // update with a new completed run is correctly treated as "new", not as a
      // second baseline seed.
      for (const run of runs) {
        if (run.conversationId && run.status === "succeeded") {
          autoMarkedRef.current.add(run.conversationId);
        }
      }
      baselineSeededRef.current = true;
      return;
    }

    // On subsequent updates, mark any succeeded run whose conversationId hasn't
    // been auto-marked yet (new completion observed by this client).
    for (const run of runs) {
      if (
        run.status === "succeeded" &&
        run.conversationId &&
        !autoMarkedRef.current.has(run.conversationId)
      ) {
        autoMarkedRef.current.add(run.conversationId);
        seedRunUnreadBaseline(
          run.conversationId,
          run.finishedAt ?? run.firedAt ?? Math.floor(Date.now() / 1000),
        );
      }
    }
  }, [runs]);

  // Resolve the agent + host display labels from their catalogs. Both are
  // best-effort: an unknown id falls back to the raw id (agent) or a friendly
  // default (host) rather than blanking the field.
  const { data: agents } = useAvailableAgents();
  const { data: hosts } = useHosts();
  const agentLabel = useMemo(() => {
    if (!task) return "";
    return agents?.find((a) => a.id === task.agentId)?.display_name ?? task.agentId;
  }, [agents, task]);
  const hostLabel = useMemo(() => {
    if (!task) return "";
    // Null hostId = the server resolves the connected host at fire time.
    if (task.hostId == null) return "Auto (connected host)";
    return hosts?.find((h) => h.host_id === task.hostId)?.name ?? task.hostId;
  }, [hosts, task]);

  if (isLoading) {
    return (
      <PageScroll contentClassName="px-6" data-testid="task-detail-loading">
        <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading task…
        </div>
      </PageScroll>
    );
  }

  // Not found (bad / deleted id → 404) or any load error: a friendly message
  // plus a way back to the list.
  if (isError || !task) {
    return (
      <PageScroll contentClassName="px-6" data-testid="task-detail-not-found">
        <BackLink />
        <div className="flex flex-col items-start gap-2 py-12">
          <p className="text-sm font-medium">This automation couldn’t be found.</p>
          <p className="text-sm text-muted-foreground">
            It may have been deleted, or the link is out of date.
          </p>
        </div>
      </PageScroll>
    );
  }

  const paused = task.state === "paused";
  const scheduleSummary = describeSchedule(task.rrule);
  const nextRun = formatNextRunAt(task.nextRunAt, now);
  // A single per-task busy flag: the header actions disable while any of this
  // task's mutations are in flight.
  const busy = updateMutation.isPending || deleteMutation.isPending || runNowMutation.isPending;

  function handlePauseToggle() {
    updateMutation.mutate({
      id: task!.id,
      input: { state: paused ? "active" : "paused" },
    });
  }

  function handleRunNow() {
    // Snapshot the pre-fire newest run id so the awaiting effect knows what
    // "a new row appeared" means.
    preFireNewestIdRef.current = runs?.[0]?.id ?? null;
    runNowMutation.mutate(task!.id, {
      onSuccess: () => {
        showToast("Run started");
        // Keep the button in loading state until the new run row appears (~1s).
        setAwaitingRunRow(true);
        // Safety cap: clear after 20s even if no row ever shows up.
        if (awaitingTimerRef.current != null) clearTimeout(awaitingTimerRef.current);
        awaitingTimerRef.current = setTimeout(clearAwaitingRunRow, 20_000);
      },
      onError: (err) => {
        clearAwaitingRunRow();
        // A 409 (CONFLICT) means a run for this task is already in flight — the
        // run is fine. Show a truthful, non-alarming message.
        if (err instanceof ScheduledTaskApiError && err.status === 409) {
          showToast("This run is already in progress");
        } else {
          showToast("Couldn't start the run");
        }
      },
    });
  }

  function handleDelete() {
    // Optimistic navigation: on success return to the list (the row is already
    // gone from the invalidated list cache).
    deleteMutation.mutate(task!.id, {
      onSuccess: () => navigate("/tasks"),
    });
  }

  return (
    <PageScroll contentClassName="px-6" data-testid="task-detail-page">
      <BackLink />

      {/* Header: title + right-aligned actions. */}
      <div className="mt-4 flex items-start justify-between gap-4">
        <h1 className="min-w-0 break-words text-2xl font-semibold" data-testid="task-detail-title">
          {task.name}
        </h1>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Edit automation"
            data-testid="task-detail-edit"
            disabled={busy}
            onClick={() => setEditOpen(true)}
          >
            <PencilIcon className="size-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Delete automation"
            data-testid="task-detail-delete"
            disabled={busy}
            onClick={handleDelete}
          >
            <Trash2Icon className="size-4" />
          </Button>
          <Button
            data-testid="task-detail-run-now"
            disabled={busy || awaitingRunRow}
            onClick={handleRunNow}
          >
            {runNowMutation.isPending || awaitingRunRow ? (
              <>
                <Loader2Icon className="size-4 animate-spin" />
                In progress
              </>
            ) : (
              <>
                <PlayIcon className="size-4" />
                Run now
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Status line: active toggle (Switch, ON = active) + state pill +
          schedule + next-run. The Switch sits to the LEFT of the pill. */}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
        {/* A scheduled task is ON when active. onCheckedChange passes a boolean,
            but handlePauseToggle derives the next state from `paused` and
            ignores its args, so no rewiring is needed. */}
        <Switch
          aria-label={paused ? "Resume automation" : "Pause automation"}
          data-testid="task-detail-pause-toggle"
          checked={!paused}
          disabled={busy}
          onCheckedChange={handlePauseToggle}
        />
        <span
          data-testid="task-detail-state-pill"
          className={cn(
            "rounded-full px-2 py-0.5 text-xs font-medium",
            paused
              ? "bg-muted text-muted-foreground"
              : "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
          )}
        >
          {paused ? "Paused" : "Active"}
        </span>
        <span className="text-muted-foreground" data-testid="task-detail-schedule">
          {scheduleSummary}
          {nextRun && ` · Next run ${nextRun}`}
        </span>
      </div>

      {/* Prompt. */}
      <Section title="Prompt">
        <p
          className="whitespace-pre-wrap text-sm text-foreground/90"
          data-testid="task-detail-prompt"
        >
          {task.prompt}
        </p>
      </Section>

      {/* Configuration: agent + host columns. */}
      <Section title="Configuration">
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <ConfigField label="Agent" value={agentLabel} testId="task-detail-agent" />
          <ConfigField label="Host" value={hostLabel} testId="task-detail-host" />
        </dl>
      </Section>

      <hr className="my-6 border-border" />

      {/* Run history. */}
      <h2 className="mb-2 text-sm font-medium text-muted-foreground">Run history</h2>
      {runsLoading ? (
        <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading runs…
        </div>
      ) : runsError ? (
        <p className="pb-2 text-sm text-muted-foreground" data-testid="task-detail-runs-error">
          Couldn’t load run history.
        </p>
      ) : (runs ?? []).length === 0 ? (
        <p className="pb-2 text-sm text-muted-foreground" data-testid="task-detail-runs-empty">
          No runs yet.
        </p>
      ) : (
        <ul className="flex flex-col gap-1" data-testid="task-detail-runs">
          {(runs ?? []).map((run) => (
            <RunRow key={run.id} run={run} now={now} />
          ))}
        </ul>
      )}

      <CreateScheduledTaskDialog open={editOpen} onOpenChange={setEditOpen} editingTask={task} />
    </PageScroll>
  );
}

function BackLink() {
  return (
    <Link
      to="/tasks"
      data-testid="task-detail-back"
      className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
    >
      <ChevronLeftIcon className="size-4" />
      Scheduled tasks
    </Link>
  );
}

/** A titled block with a small muted-caps label above its content. */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h2 className="mb-2 text-sm font-medium text-muted-foreground">{title}</h2>
      {children}
    </section>
  );
}

function ConfigField({ label, value, testId }: { label: string; value: string; testId: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground uppercase tracking-wide">{label}</dt>
      <dd className="truncate text-sm text-foreground" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}

/**
 * Resolve whether a run's conversation is genuinely UNREAD from the enriched
 * run payload — with zero per-row session GETs.
 *
 * The runs endpoint returns `conversationUpdatedAt`, `conversationStatus`, and
 * `viewerUnread` (batched server-side). Priority:
 *   1. Local explicit-unread override (app-level "mark unread", a628dc12 path).
 *   2. Local `isConversationUnseen` — fires once `lastSeenMap` has a baseline
 *      for this conversation (set by `seedRunUnreadBaseline` on new completions,
 *      or by the sidebar's `seedReadState` on load).
 *   3. Server `viewerUnread` — the server-side explicit-unread flag, covering
 *      runs that were completed before this page was ever opened (no local
 *      baseline yet).
 * `useUnseenTick()` re-renders the instant the user opens the thread (marking
 * it read in the local mirror) so the dot clears without a refetch.
 */
function useRunUnread(run: ScheduledTaskRun): boolean {
  // Re-render when the read-state mirror changes (open thread → mark read).
  useUnseenTick();
  if (run.conversationId === null || run.conversationUpdatedAt === null) return false;
  if (isExplicitlyUnread(run.conversationId)) return true;
  if (
    isConversationUnseen(
      run.conversationId,
      run.conversationUpdatedAt,
      run.conversationStatus ?? undefined,
    )
  ) {
    return true;
  }
  // Fall back to the server's explicit-unread flag for runs the local mirror
  // hasn't seeded yet (no lastSeenMap entry from this client session).
  return run.viewerUnread === true;
}

/**
 * One run in the history list. Layout (LEFT status column + whole-row click):
 * - LEADING: a single status icon chosen by priority (see `RunStatusIcon`) —
 *   failed triangle > skipped circle-slash > succeeded-unread blue dot >
 *   succeeded-read grey dot — each with an explanatory tooltip.
 * - BODY: the fire timestamp (bold), the duration (muted), and — for
 *   skipped/failed — an errorCode-derived message. No fabricated summary.
 *
 * When the run produced a conversation the ENTIRE row is the click target
 * (a real `<Link>`: keyboard-focusable, Enter/Space activate natively) that
 * navigates to `/c/:conversationId`, with a hover highlight + pointer cursor.
 * Runs without a conversation (e.g. skipped) render as a plain, non-interactive
 * row: no highlight, no pointer, not focusable.
 */
function RunRow({ run, now }: { run: ScheduledTaskRun; now: Date }) {
  const timestamp = formatRunTimestamp(run.firedAt ?? run.scheduledAt, now);
  const duration = formatRunDuration(run.firedAt, run.finishedAt);
  // Unread only applies to a succeeded run that produced a conversation.
  const unread = useRunUnread(run);

  const body = (
    <>
      <span className="flex size-5 shrink-0 items-center justify-center">
        <RunStatusIcon run={run} unread={unread} />
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex items-baseline gap-2">
          <span className="text-sm font-normal">{timestamp ?? "—"}</span>
          {duration && (
            <span className="text-xs text-muted-foreground" data-testid="run-duration">
              {duration}
            </span>
          )}
        </span>
      </span>
    </>
  );

  const commonClassName = "flex items-center gap-3 rounded-lg px-2 py-2";

  // Clickable when the run has a conversation to open. A real <Link> (anchor)
  // is focusable and Enter/Space-activatable for free; the hover highlight +
  // pointer cursor signal clickability without any explicit link text.
  if (run.conversationId) {
    return (
      <li data-testid="task-detail-run" data-run-status={run.status} data-run-unread={unread}>
        <Link
          to={`/c/${run.conversationId}`}
          data-testid="run-open"
          aria-label="Open conversation"
          className={cn(
            commonClassName,
            "cursor-pointer transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none",
          )}
        >
          {body}
        </Link>
      </li>
    );
  }

  // No conversation → not clickable, no hover highlight, not focusable.
  return (
    <li
      data-testid="task-detail-run"
      data-run-status={run.status}
      data-run-unread={run.status === "succeeded" ? unread : undefined}
      className={commonClassName}
    >
      {body}
    </li>
  );
}

/**
 * The leading LEFT status icon for a run. Exactly one renders, by priority:
 *   1. failed              → amber warning triangle, tooltip = errorCode message.
 *   2. skipped             → muted calendar-off icon, tooltip = skip reason.
 *   3. running             → spinning loader icon, tooltip = "Running".
 *   4. succeeded + UNREAD  → blue filled dot, tooltip = "Unread".
 *   5. succeeded + READ / no conversation → muted grey dot, tooltip = "Completed".
 *   6. scheduled/incomplete → muted grey dot, no tooltip (not terminal).
 */
function RunStatusIcon({ run, unread }: { run: ScheduledTaskRun; unread: boolean }) {
  if (run.status === "failed") {
    return (
      <IndicatorWithTooltip tooltip={describeRunError(run.errorCode, "failed")}>
        <TriangleAlertIcon
          data-testid="run-status-icon"
          data-run-icon="failed"
          className="size-4 shrink-0 text-amber-500"
          strokeWidth={1.75}
        />
      </IndicatorWithTooltip>
    );
  }
  if (run.status === "skipped") {
    return (
      <IndicatorWithTooltip tooltip={describeRunError(run.errorCode, "skipped")}>
        <CalendarOffIcon
          data-testid="run-status-icon"
          data-run-icon="skipped"
          className="size-4 shrink-0 text-muted-foreground"
          strokeWidth={1.75}
        />
      </IndicatorWithTooltip>
    );
  }
  if (run.status === "running") {
    return (
      <IndicatorWithTooltip tooltip="Running">
        <Loader2Icon
          data-testid="run-status-icon"
          data-run-icon="running"
          className="size-3.5 shrink-0 animate-spin text-muted-foreground"
        />
      </IndicatorWithTooltip>
    );
  }
  if (run.status === "succeeded" && unread) {
    return (
      <IndicatorWithTooltip tooltip="Unread">
        <span
          aria-hidden
          data-testid="run-status-dot"
          data-run-icon="unread"
          data-run-unread={true}
          className="size-2 shrink-0 rounded-full bg-brand-accent"
        />
      </IndicatorWithTooltip>
    );
  }
  // Succeeded+read gets a tooltip ("Completed"); non-terminal statuses share the
  // same muted dot but with no tooltip (nothing meaningful to say yet).
  const dot = (
    <span
      aria-hidden
      data-testid="run-status-dot"
      data-run-icon={run.status === "succeeded" ? "read" : "pending"}
      data-run-unread={run.status === "succeeded" ? false : undefined}
      className="size-2 shrink-0 rounded-full bg-muted-foreground/40"
    />
  );
  if (run.status === "succeeded") {
    return <IndicatorWithTooltip tooltip="Completed">{dot}</IndicatorWithTooltip>;
  }
  return dot;
}

/** Wrap a status indicator in the shared Tooltip (app already provides the
 * TooltipProvider). The trigger uses `asChild` so it renders the child element
 * directly (icon/dot) rather than a nested button. */
function IndicatorWithTooltip({
  tooltip,
  children,
}: {
  tooltip: string;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* A span wrapper keeps the trigger focusable/hoverable for both the
            svg icon and the pure-CSS dot span. Vertical alignment to the
            timestamp baseline is set per-child (icons vs. the smaller dot). */}
        <span className="inline-flex shrink-0">{children}</span>
      </TooltipTrigger>
      <TooltipContent data-testid="run-status-tooltip">{tooltip}</TooltipContent>
    </Tooltip>
  );
}
