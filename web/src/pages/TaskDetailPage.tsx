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

import { useMemo, useState } from "react";
import {
  ArrowRightIcon,
  ChevronLeftIcon,
  Loader2Icon,
  PauseIcon,
  PencilIcon,
  PlayIcon,
  Trash2Icon,
} from "lucide-react";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import {
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTask,
  useScheduledTaskRuns,
  useUpdateScheduledTask,
} from "@/hooks/useScheduledTasks";
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
import type { ScheduledTaskRun, ScheduledTaskRunStatus } from "@/lib/scheduledTasksApi";
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
    runNowMutation.mutate(task!.id);
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
          <Button data-testid="task-detail-run-now" disabled={busy} onClick={handleRunNow}>
            {runNowMutation.isPending ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              <PlayIcon className="size-4" />
            )}
            Run now
          </Button>
        </div>
      </div>

      {/* Status line: pause/resume toggle + state pill + schedule + next-run. */}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
        <Button
          variant="outline"
          size="icon-sm"
          aria-label={paused ? "Resume automation" : "Pause automation"}
          data-testid="task-detail-pause-toggle"
          disabled={busy}
          onClick={handlePauseToggle}
        >
          {paused ? <PlayIcon className="size-4" /> : <PauseIcon className="size-4" />}
        </Button>
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
      <h2 className="mb-3 text-sm font-medium text-muted-foreground">Run history</h2>
      {runsLoading ? (
        <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading runs…
        </div>
      ) : runsError ? (
        <p className="py-6 text-sm text-muted-foreground" data-testid="task-detail-runs-error">
          Couldn’t load run history.
        </p>
      ) : (runs ?? []).length === 0 ? (
        <p className="py-6 text-sm text-muted-foreground" data-testid="task-detail-runs-empty">
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
 * One run in the history list: a status dot, the fire timestamp (bold), the
 * duration (muted), and a detail line — an errorCode-derived message for
 * skipped/failed runs, or an "Open conversation →" link when the run produced
 * a conversation. No fabricated per-run summary.
 */
function RunRow({ run, now }: { run: ScheduledTaskRun; now: Date }) {
  const timestamp = formatRunTimestamp(run.firedAt ?? run.scheduledAt, now);
  const duration = formatRunDuration(run.firedAt, run.finishedAt);
  const detail = runDetail(run);

  return (
    <li
      data-testid="task-detail-run"
      data-run-status={run.status}
      className="flex items-start gap-3 rounded-lg px-2 py-2"
    >
      <span
        aria-hidden
        data-testid="run-status-dot"
        className={cn("mt-1.5 size-2 shrink-0 rounded-full", statusDotClass(run.status))}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-baseline gap-2">
          <span className="text-sm font-semibold">{timestamp ?? "—"}</span>
          {duration && (
            <span className="text-xs text-muted-foreground" data-testid="run-duration">
              {duration}
            </span>
          )}
        </div>
        {detail}
      </div>
    </li>
  );
}

/**
 * The run's detail line. Skipped/failed → a human message from the errorCode;
 * otherwise, if the run produced a conversation, an "Open conversation →" link.
 * Returns null when there's nothing honest to show (e.g. a scheduled/running
 * run with no conversation yet).
 */
function runDetail(run: ScheduledTaskRun): React.ReactNode {
  if (run.status === "skipped" || run.status === "failed") {
    return (
      <span className="text-xs text-muted-foreground" data-testid="run-error-detail">
        {describeRunError(run.errorCode, run.status)}
      </span>
    );
  }
  if (run.conversationId) {
    return (
      <Link
        to={`/c/${run.conversationId}`}
        data-testid="run-open-conversation"
        className="inline-flex w-fit items-center gap-1 text-xs text-primary transition-colors hover:underline"
      >
        Open conversation
        <ArrowRightIcon className="size-3" />
      </Link>
    );
  }
  return null;
}

/** Status → colored dot. Mirrors the list's completion-badge color semantics. */
function statusDotClass(status: ScheduledTaskRunStatus): string {
  switch (status) {
    case "succeeded":
      return "bg-emerald-500";
    case "failed":
      return "bg-destructive";
    case "skipped":
      return "bg-amber-500";
    default:
      // scheduled / running / incomplete — not yet a terminal signal.
      return "bg-muted-foreground/40";
  }
}
