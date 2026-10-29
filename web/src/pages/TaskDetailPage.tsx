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
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTask,
  useScheduledTaskRuns,
  useUpdateScheduledTask,
} from "@/hooks/useScheduledTasks";
import {
  isConversationUnread,
  seedRunUnreadBaseline,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { useNow } from "@/hooks/useNow";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import {
  describeRunFailure,
  describeSchedule,
  formatNextRunAt,
  formatRunDuration,
  formatRunTimestamp,
} from "@/lib/scheduleText";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { ScheduledTaskApiError } from "@/lib/scheduledTasksApi";
import type { ScheduledTaskRun } from "@/lib/scheduledTasksApi";
import { cn } from "@/lib/utils";

export function TaskDetailPage() {
  const { taskId = "" } = useParams<{ taskId: string }>();
  // Remount per task: the run-tracking state below belongs to one task's
  // history, and moving between tasks in browser history can keep this route
  // mounted.
  return <TaskDetail key={taskId} taskId={taskId} />;
}

function TaskDetail({ taskId }: { taskId: string }) {
  const navigate = useNavigate();
  const now = useNow();

  // "Awaiting new run row" loading state for the Run now button. The button
  // stays in its loading state after the POST returns 202 until a new run row
  // arrives (newest id changes) or the safety cap fires. Declared BEFORE the
  // runs query because it also keeps that query's refetchInterval alive across
  // the gap between the 202 and the server writing the row.
  const [awaitingRunRow, setAwaitingRunRow] = useState(false);
  const preFireNewestIdRef = useRef<string | null>(null);
  const awaitingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const { data: task, isLoading, isError, error, refetch } = useScheduledTask(taskId);
  // Run history only fetches once we have a real id.
  const {
    data: runs,
    isLoading: runsLoading,
    isError: runsError,
    isFetchedAfterMount: runsFetchedAfterMount,
  } = useScheduledTaskRuns(taskId, taskId !== "", awaitingRunRow);

  const updateMutation = useUpdateScheduledTask();
  const deleteMutation = useDeleteScheduledTask();
  const runNowMutation = useRunScheduledTaskNow();

  const [editOpen, setEditOpen] = useState(false);

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
  }, [runs, awaitingRunRow]);

  // Unmount cleanup for the local safety timer so it can't fire into an
  // unmounted page. The run-history refresh needs no cleanup here: it's a
  // `refetchInterval` on `useScheduledTaskRuns`, which TanStack Query tears
  // down automatically when this page unmounts.
  useEffect(() => {
    return () => {
      if (awaitingTimerRef.current != null) clearTimeout(awaitingTimerRef.current);
    };
  }, [taskId]);

  // A run that succeeds while this page is open is marked unread until its
  // session is opened. Runs that had already finished when the page loaded
  // keep their session's own read state, as in the sidebar, which treats a
  // session this client has never seen as read on first load.
  //
  // Two guards prevent incorrect behaviour:
  //   1. ONCE-PER-SESSION: `autoMarkedRef` tracks every conversationId this
  //      component has already auto-marked. Re-marking on a subsequent poll
  //      would resurrect the dot for a run the user already opened.
  //   2. NO-RETROACTIVE: the ref is seeded from the first runs response fetched
  //      after this page mounts, with all currently-terminal ids, so history is
  //      never mass-marked on load. Cached data doesn't count: a run cached as
  //      running may have finished while the page was closed. Only runs that
  //      turn terminal after the baseline trigger a mark.
  const autoMarkedRef = useRef<Set<string>>(new Set());
  const baselineSeededRef = useRef(false);
  useEffect(() => {
    if (!runs || !runsFetchedAfterMount) return;

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
        run.conversationUpdatedAt != null &&
        !autoMarkedRef.current.has(run.conversationId)
      ) {
        autoMarkedRef.current.add(run.conversationId);
        // Seed against the timestamp the unread check compares with; the run's
        // finishedAt is written separately and can land a second later.
        seedRunUnreadBaseline(run.conversationId, run.conversationUpdatedAt);
      }
    }
  }, [runs, runsFetchedAfterMount]);

  // Older servers resolve pins through session discovery; current catalogs
  // can hide legacy IDs that existing tasks still use.
  const {
    data: agents,
    isPlaceholderData: agentsArePartial,
    isError: agentsError,
  } = useAvailableAgents({
    pinnedAgentIds: task != null ? [task.agentId] : [],
  });
  const { data: hosts } = useHosts();
  const info = useServerInfo();
  const sandboxLabel = sandboxOptionLabel(info !== "loading" ? info.sandbox_provider : null);
  const agentLabel = useMemo(() => {
    if (!task) return "";
    const agent = agents?.find((a) => a.id === task.agentId);
    if (agent) return agent.display_name;
    if (agents === undefined || agentsArePartial || agentsError) return task.agentId;
    return `Agent not in current list (${task.agentId})`;
  }, [agents, agentsArePartial, agentsError, task]);
  const hostLabel = useMemo(() => {
    if (!task) return "";
    if (task.executionTarget === "managed_sandbox") return sandboxLabel;
    // Null hostId = the server resolves the connected host at fire time. Same
    // wording as the edit dialog's host picker.
    if (task.hostId == null) return "Resolve at fire time";
    return hosts?.find((h) => h.host_id === task.hostId)?.name ?? task.hostId;
  }, [hosts, sandboxLabel, task]);

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

  // A failed request (network, server error) can be retried; only a 404 means
  // the automation is gone.
  if (isError && !(error instanceof ScheduledTaskApiError && error.status === 404)) {
    return (
      <PageScroll contentClassName="px-6" data-testid="task-detail-load-error">
        <BackLink />
        <div className="flex flex-col items-start gap-2 py-12">
          <p className="text-sm font-medium">Couldn’t load this automation.</p>
          <Button
            variant="outline"
            size="sm"
            data-testid="task-detail-retry"
            onClick={() => void refetch()}
          >
            Try again
          </Button>
        </div>
      </PageScroll>
    );
  }

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
        // Keep the button in loading state until the new run row appears, and —
        // because the row is written a few seconds AFTER this 202 — keep the run
        // history polling across that gap so the row is picked up at all.
        setAwaitingRunRow(true);
        // Safety cap: clear after 20s even if no row ever shows up. Generous
        // next to the observed few-second write, and only this window is
        // unconditional — once the row lands, run status drives the polling.
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
        {/* ON = active. handlePauseToggle derives the next state from `paused`. */}
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
      Automations
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
 * The runs endpoint returns the session's read state alongside each run
 * (batched server-side); `isConversationUnread` applies the sidebar's rules to
 * it. `useUnseenTick()` re-renders the instant the user opens the thread
 * (marking it read in the local mirror) so the dot clears without a refetch.
 */
function useRunUnread(run: ScheduledTaskRun): boolean {
  // Re-render when the read-state mirror changes (open thread → mark read).
  useUnseenTick();
  if (run.conversationId === null || run.conversationUpdatedAt === null) return false;
  return isConversationUnread(
    run.conversationId,
    run.conversationUpdatedAt,
    run.conversationStatus ?? undefined,
    { lastSeen: run.viewerLastSeen, unread: run.viewerUnread },
  );
}

/**
 * One run in the history list. Layout (LEFT status column + whole-row click):
 * - LEADING: a single status icon chosen by priority (see `RunStatusIcon`) —
 *   failed triangle > skipped circle-slash > succeeded-unread blue dot >
 *   succeeded-read grey dot — each with an explanatory tooltip.
 * - BODY: the fire timestamp, the duration (muted), and — for skipped/failed —
 *   the stage the fire stopped at and why, derived from `errorCode`.
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
  const failure = isUnsuccessfulRun(run) ? describeRunFailure(run.errorCode, run.status) : null;
  const inFlightLabel = IN_FLIGHT_RUN_LABELS[run.status];

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
          {inFlightLabel && (
            <span className="text-xs text-muted-foreground" data-testid="run-status-label">
              {inFlightLabel}
            </span>
          )}
        </span>
        {failure && (
          <span className="text-xs text-muted-foreground" data-testid="run-failure">
            {failure.stage && (
              <span className="font-medium text-foreground/80" data-testid="run-failure-stage">
                {failure.stage}
              </span>
            )}
            {failure.stage && " · "}
            {failure.reason}
          </span>
        )}
      </span>
    </>
  );

  const commonClassName = cn(
    "flex gap-3 rounded-lg px-2 py-2",
    failure ? "items-start" : "items-center",
  );

  // Clickable when the run has a conversation to open. A real <Link> (anchor)
  // is focusable and Enter/Space-activatable for free; the hover highlight +
  // pointer cursor signal clickability without any explicit link text.
  if (run.conversationId) {
    return (
      <li data-testid="task-detail-run" data-run-status={run.status} data-run-unread={unread}>
        <Link
          to={`/c/${run.conversationId}`}
          data-testid="run-open"
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
 *   1. failed / incomplete → amber warning triangle, tooltip = "Failed".
 *   2. skipped             → muted calendar-off icon, tooltip = "Skipped".
 *   3. starting / running  → spinning loader icon, tooltip = the status.
 *   4. succeeded + UNREAD  → blue filled dot, tooltip = "Unread".
 *   5. succeeded + READ / no conversation → muted grey dot, tooltip = "Completed".
 *   6. scheduled           → muted grey dot, no tooltip (not started yet).
 */
function RunStatusIcon({ run, unread }: { run: ScheduledTaskRun; unread: boolean }) {
  if (run.status === "failed" || run.status === "incomplete") {
    return (
      <IndicatorWithTooltip tooltip="Failed">
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
      <IndicatorWithTooltip tooltip="Skipped">
        <CalendarOffIcon
          data-testid="run-status-icon"
          data-run-icon="skipped"
          className="size-4 shrink-0 text-muted-foreground"
          strokeWidth={1.75}
        />
      </IndicatorWithTooltip>
    );
  }
  if (run.status === "starting" || run.status === "running") {
    return (
      <IndicatorWithTooltip
        tooltip={run.status === "starting" ? "Starting" : "Running"}
        announce={false}
      >
        <Loader2Icon
          data-testid="run-status-icon"
          data-run-icon={run.status}
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

// A run that hasn't finished says what it's doing; the spinner alone doesn't
// tell a run still starting its sandbox from one whose agent is working.
const IN_FLIGHT_RUN_LABELS: Partial<Record<ScheduledTaskRun["status"], string>> = {
  scheduled: "Queued",
  starting: "Starting",
  running: "Running",
};

/** Whether a run ended without succeeding, so its row explains why. */
function isUnsuccessfulRun(run: ScheduledTaskRun): boolean {
  return run.status === "failed" || run.status === "skipped" || run.status === "incomplete";
}

/** Wrap a status indicator in the shared Tooltip (app already provides the
 * TooltipProvider). The trigger uses `asChild` so it renders the child element
 * directly (icon/dot) rather than a nested button. */
function IndicatorWithTooltip({
  tooltip,
  announce = true,
  children,
}: {
  tooltip: string;
  /** Also put the label in the row's accessible text; off where the row already shows it. */
  announce?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* A span wrapper keeps the trigger focusable/hoverable for both the
            svg icon and the pure-CSS dot span. Vertical alignment to the
            timestamp baseline is set per-child (icons vs. the smaller dot). */}
        <span className="inline-flex shrink-0">
          {children}
          {announce && <span className="sr-only">{tooltip}</span>}
        </span>
      </TooltipTrigger>
      <TooltipContent data-testid="run-status-tooltip">{tooltip}</TooltipContent>
    </Tooltip>
  );
}
