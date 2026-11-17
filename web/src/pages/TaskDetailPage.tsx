/**
 * Scheduled-task DETAIL page (`/tasks/:taskId`) — opened by clicking a task
 * card on the `/tasks` list. Shows the task's header (title + share / edit /
 * delete / Run now), a status line (pause-resume toggle + Active/Paused pill + the
 * human-readable schedule + relative next-run), the Prompt, a Configuration
 * block with saved launch settings, and the run history.
 *
 * All data + mutations come from the existing scheduled-tasks hooks; editing
 * reuses `CreateScheduledTaskDialog` in its edit mode. For an automation shared
 * with the viewer the page names its owner and is read-only: every action that
 * changes it shows disabled with a tooltip, and each run shows its outcome, without
 * its session's cost, approvals or tasks. Nothing here is fabricated: each run
 * renders only the fields the server actually returns (status, timestamp,
 * duration, an errorCode-derived message, and an open-conversation link) — never
 * an invented summary line.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  CalendarOffIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  Loader2Icon,
  PencilIcon,
  PlayIcon,
  ShareIcon,
  Trash2Icon,
  TriangleAlertIcon,
} from "lucide-react";
import { Link, useNavigate, useParams, useRebasePath, useSearchParams } from "@/lib/routing";
import { getShareableSessionLink } from "@/lib/shareLink";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { showToast } from "@/components/ui/toast";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import {
  automationOwnerName,
  isSharedWithViewer,
  OWNER_ONLY_REASONS,
} from "@/components/scheduled/ownerOnlyActions";
import { DisabledActionTooltip } from "@/components/DisabledActionTooltip";
import { PermissionsModal } from "@/components/PermissionsModal";
import {
  decodeTriggerPrompt,
  hasTriggerHeader,
  isTriggerPrompt,
  SESSION_LINK_ID_PLACEHOLDER,
  splitTriggerPrompt,
  stripTriggerHeader,
} from "@/components/scheduled/triggerTemplates";
import {
  RunGraphButton,
  RunPlanButton,
  RunTimeline,
  RunWorkflowButton,
  useRunTaskMenus,
} from "@/components/workflows/RunTasks";
import { planRunControl } from "@/shell/PlanRunButton";
import { WorkflowGraphOverlay } from "@/components/workflows/WorkflowStepsGraph";
import {
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTask,
  scheduledTaskRunsKey,
  useScheduledTaskRuns,
  useScheduledTaskRunSteps,
  useUpdateScheduledTask,
} from "@/hooks/useScheduledTasks";
import {
  isConversationUnread,
  seedRunUnreadBaseline,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { useNow } from "@/hooks/useNow";
import { useSession } from "@/hooks/useSession";
import { useViewerId } from "@/hooks/useViewerId";
import { useSessionWorking } from "@/hooks/useSessionWorking";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { useProjects } from "@/hooks/useConversations";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { nativeCodingAgentForAvailableAgent } from "@/lib/nativeCodingAgents";
import { canonicalNativeHarness, nativePermissionOptions } from "@/lib/nativeHarnessModes";
import { formatStatusEffortLabel, formatStatusModelLabel } from "@/lib/composerModelLabel";
import { formatSessionCostUsd } from "@/lib/formatCost";
import {
  describeRunFailure,
  describeSchedule,
  formatNextRunAt,
  formatRunDuration,
  formatRunTimestamp,
} from "@/lib/scheduleText";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { AUTOMATION_SHARE_PARAM } from "@/lib/automationFromSession";
import {
  isFeatureEnabled,
  isSharingOff,
  sandboxOptionLabel,
  SHARING_OFF_REASON,
} from "@/lib/capabilities";
import { canShareAutomation } from "@/lib/permissionsApi";
import { ScheduledTaskApiError } from "@/lib/scheduledTasksApi";
import type { ScheduledTaskRun, ScheduledTaskRunSteps } from "@/lib/scheduledTasksApi";
import {
  latestStatusText,
  stepProgress,
  WAITING_FOR_YOU,
  type StepStatus,
} from "@/lib/sessionJournal";
import { cn } from "@/lib/utils";

const RUN_HIGHLIGHT_DURATION_MS = 3_000;
const RUN_HIGHLIGHT_WAIT_MS = 120_000;

export function TaskDetailPage() {
  const { taskId = "" } = useParams<{ taskId: string }>();
  // Remount per task: the run-tracking state below belongs to one task's
  // history, and moving between tasks in browser history can keep this route
  // mounted.
  return <TaskDetail key={taskId} taskId={taskId} />;
}

function TaskDetail({ taskId }: { taskId: string }) {
  const navigate = useNavigate();
  const info = useServerInfo();
  const now = useNow();
  // Saving or switching a trigger re-renders its prompt, whose workers post this share link.
  const rebasePath = useRebasePath();
  const sessionLink = getShareableSessionLink(SESSION_LINK_ID_PLACEHOLDER, rebasePath);

  // "Awaiting new run row" loading state for the Run now button. The button
  // stays in its loading state after the POST returns 202 until a new run row
  // arrives (newest id changes) or the safety cap fires. Declared BEFORE the
  // runs query because it also keeps that query's refetchInterval alive across
  // the gap between the 202 and the server writing the row.
  const [awaitingRunRow, setAwaitingRunRow] = useState(false);
  const preFireNewestIdRef = useRef<string | null>(null);
  const awaitingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRunHighlightRef = useRef<{ runId: string; until: number } | null>(null);
  const [highlightedRunId, setHighlightedRunId] = useState<string | null>(null);

  const { data: task, isLoading, isError, error, refetch } = useScheduledTask(taskId);
  // Run history only fetches once we have a real id.
  const {
    data: runs,
    isLoading: runsLoading,
    isError: runsError,
    isFetchedAfterMount: runsFetchedAfterMount,
  } = useScheduledTaskRuns(taskId, taskId !== "", awaitingRunRow);
  // Plans are session content, which a viewer's run history leaves out. The owner's
  // steps load alongside the task; the server sends a viewer none either way.
  const runStepsEnabled =
    isFeatureEnabled(info, "workflows") && !(task != null && isSharedWithViewer(task));
  const runSteps = useScheduledTaskRunSteps(taskId, runs, runStepsEnabled);

  const updateMutation = useUpdateScheduledTask();
  const deleteMutation = useDeleteScheduledTask();
  const runNowMutation = useRunScheduledTaskNow();

  const [editOpen, setEditOpen] = useState(false);
  const viewerId = useViewerId();
  const [shareOpen, setShareOpen] = useState(false);
  // A session's share dialog links here with the share parameter to open this one.
  const [searchParams, setSearchParams] = useSearchParams();
  const shareRequested = searchParams.get(AUTOMATION_SHARE_PARAM) === "1";

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
    const pendingHighlight = pendingRunHighlightRef.current;
    if (pendingHighlight != null) {
      if (Date.now() >= pendingHighlight.until) {
        pendingRunHighlightRef.current = null;
      } else if (runs?.some((run) => run.id === pendingHighlight.runId)) {
        pendingRunHighlightRef.current = null;
        setHighlightedRunId(pendingHighlight.runId);
        clearAwaitingRunRow();
        return;
      }
    }
    // Older servers omit the run ID; only use the newest row to settle loading.
    if (!awaitingRunRow || pendingRunHighlightRef.current != null) return;
    const newestId = runs?.[0]?.id ?? null;
    if (newestId != null && newestId !== preFireNewestIdRef.current) {
      clearAwaitingRunRow();
    }
  }, [runs, awaitingRunRow]);

  useEffect(() => {
    if (highlightedRunId == null) return;
    const timer = setTimeout(() => setHighlightedRunId(null), RUN_HIGHLIGHT_DURATION_MS);
    return () => clearTimeout(timer);
  }, [highlightedRunId]);

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

  // Resolve the agent + host display labels from their catalogs. Both are
  // best-effort: an unknown id falls back to the raw id (agent) or a friendly
  // default (host) rather than blanking the field.
  // Pin the task's agent: another agent with the same name (e.g. a session's
  // own copy) would otherwise win the catalog's name merge and leave the
  // label showing a raw id.
  const { data: agents } = useAvailableAgents({
    pinnedAgentIds: task != null ? [task.agentId] : [],
  });
  const { data: hosts } = useHosts();
  const { data: projects } = useProjects({ enabled: task?.projectId != null });
  const sandboxLabel = sandboxOptionLabel(info !== "loading" ? info.sandbox_provider : null);
  const agent = agents?.find((candidate) => candidate.id === task?.agentId);
  const agentLabel = agent?.display_name ?? task?.agentId ?? "";
  const agentHarness = nativeCodingAgentForAvailableAgent(agent)?.harness ?? agent?.harness;
  const harness = agentHarness ? canonicalNativeHarness(agentHarness) : null;
  const hostLabel = useMemo(() => {
    if (!task) return "";
    if (task.executionTarget === "managed_sandbox") return sandboxLabel;
    // Null hostId = the server picks the owner's live host when the task fires.
    // Same wording as the edit dialog's host picker.
    if (task.hostId == null) return "Most recently active host";
    const name = hosts?.find((h) => h.host_id === task.hostId)?.name;
    // A viewer can't see the owner's hosts.
    return name ?? (isSharedWithViewer(task) ? "The owner's host" : task.hostId);
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

  const triggerMode = task.sessionMode === "continue" && isTriggerPrompt(task.prompt);
  const paused = task.state === "paused";
  const triggerConfig = triggerMode ? decodeTriggerPrompt(task.prompt) : null;
  const legacyParts = splitTriggerPrompt(task.prompt);
  // Generated template text is hidden only where it can be told apart from the
  // user's instructions; otherwise the page shows the prompt the model runs.
  const instructionsOnly =
    triggerConfig !== null ||
    (triggerMode && !hasTriggerHeader(task.prompt) && legacyParts.systemPrompt !== null);
  const shownPrompt =
    triggerConfig?.instructions ??
    (instructionsOnly ? legacyParts.userPrompt : stripTriggerHeader(task.prompt));
  const permissionOption = nativePermissionOptions(harness).find(
    (option) => option.value === task.permissionMode,
  );
  // Outside Claude, the default permission choice leaves the CLI configuration unchanged.
  const permissionLabel =
    !task.permissionMode || (permissionOption?.value === "default" && harness !== "claude-native")
      ? "Agent default"
      : (permissionOption?.label ?? task.permissionMode);
  const scheduleSummary = describeSchedule(task.rrule, task.timezone);
  const readOnly = isSharedWithViewer(task);
  const canShare = canShareAutomation(info, viewerId, task.ownerUserId);
  // As for sessions, sharing off disables Share rather than hiding it.
  const sharingOff = isSharingOff(info);
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
    pendingRunHighlightRef.current = null;
    // Snapshot the pre-fire newest run id so the awaiting effect knows what
    // "a new row appeared" means.
    preFireNewestIdRef.current = runs?.[0]?.id ?? null;
    runNowMutation.mutate(task!.id, {
      onSuccess: (runId) => {
        showToast("Run triggered, it will start shortly");
        // Keep tracking the new row even after the button's loading timeout.
        pendingRunHighlightRef.current = runId
          ? { runId, until: Date.now() + RUN_HIGHLIGHT_WAIT_MS }
          : null;
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
        pendingRunHighlightRef.current = null;
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

  function handleShareOpenChange(next: boolean) {
    setShareOpen(next);
    if (!next && shareRequested) {
      setSearchParams(
        (current) => {
          const params = new URLSearchParams(current);
          params.delete(AUTOMATION_SHARE_PARAM);
          return params;
        },
        { replace: true },
      );
    }
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
        <div className="flex min-w-0 flex-col gap-1">
          <h1
            className="min-w-0 break-words text-2xl font-semibold"
            data-testid="task-detail-title"
          >
            {task.name}
          </h1>
          {readOnly && (
            <p className="text-sm text-muted-foreground" data-testid="task-detail-owner">
              Shared by {automationOwnerName(task)}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {readOnly ? (
            <DisabledActionTooltip reason={OWNER_ONLY_REASONS.share} label="Share automation">
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Share automation"
                data-testid="task-detail-share"
                disabled
              >
                <ShareIcon className="size-4" />
              </Button>
            </DisabledActionTooltip>
          ) : (
            canShare &&
            (sharingOff ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  {/* Disabled buttons don't receive pointer events, so the wrapper
                      owns hover/focus for the explanatory tooltip. */}
                  <span
                    tabIndex={0}
                    aria-label={`Share automation disabled: ${SHARING_OFF_REASON}`}
                    className="inline-flex"
                  >
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label="Share automation"
                      data-testid="task-detail-share"
                      disabled
                    >
                      <ShareIcon className="size-4" />
                    </Button>
                  </span>
                </TooltipTrigger>
                <TooltipContent>{SHARING_OFF_REASON}</TooltipContent>
              </Tooltip>
            ) : (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Share automation"
                data-testid="task-detail-share"
                onClick={() => setShareOpen(true)}
              >
                <ShareIcon className="size-4" />
              </Button>
            ))
          )}
          <OwnerOnly readOnly={readOnly} label="Edit automation" reason={OWNER_ONLY_REASONS.edit}>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Edit automation"
              data-testid="task-detail-edit"
              disabled={busy || readOnly}
              onClick={() => setEditOpen(true)}
            >
              <PencilIcon className="size-4" />
            </Button>
          </OwnerOnly>
          <OwnerOnly
            readOnly={readOnly}
            label="Delete automation"
            reason={OWNER_ONLY_REASONS.delete}
          >
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Delete automation"
              data-testid="task-detail-delete"
              disabled={busy || readOnly}
              onClick={handleDelete}
            >
              <Trash2Icon className="size-4" />
            </Button>
          </OwnerOnly>
          {!triggerMode && (
            <OwnerOnly readOnly={readOnly} label="Run now" reason={OWNER_ONLY_REASONS.runNow}>
              <Button
                data-testid="task-detail-run-now"
                disabled={busy || awaitingRunRow || readOnly}
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
            </OwnerOnly>
          )}
        </div>
      </div>

      {/* Status line: active toggle (Switch, ON = active) + state pill +
          schedule + next-run. The Switch sits to the LEFT of the pill. */}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
        {/* ON = active. handlePauseToggle derives the next state from `paused`. */}
        <OwnerOnly
          readOnly={readOnly}
          label={paused ? "Resume automation" : "Pause automation"}
          reason={paused ? OWNER_ONLY_REASONS.resume : OWNER_ONLY_REASONS.pause}
        >
          <Switch
            aria-label={paused ? "Resume automation" : "Pause automation"}
            data-testid="task-detail-pause-toggle"
            checked={!paused}
            disabled={busy || readOnly}
            onCheckedChange={handlePauseToggle}
          />
        </OwnerOnly>
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
          {triggerMode ? "Runs when new items match" : scheduleSummary}
          {!triggerMode && nextRun && ` · Next run ${nextRun}`}
        </span>
      </div>

      {task.sourceSessionId && (
        <Section title="Starts from">
          {readOnly ? (
            // The copy is the owner's own session.
            <p className="text-sm text-muted-foreground" data-testid="task-detail-starts-from">
              Each run starts with a copy of a session the owner chose.
            </p>
          ) : (
            <>
              <SourceSessionLink sessionId={task.sourceSessionId} />
              <p className="mt-1 text-sm text-muted-foreground">
                Each run starts with a copy of this session&apos;s history.
              </p>
            </>
          )}
        </Section>
      )}

      {/* Prompt. */}
      <Section
        title={
          task.sourceSessionId
            ? "Additional instructions"
            : instructionsOnly
              ? "Instructions"
              : "Prompt"
        }
      >
        <p
          className="whitespace-pre-wrap text-sm text-foreground/90"
          data-testid="task-detail-prompt"
        >
          {task.sourceSessionId && shownPrompt.trim() === "" ? "None" : shownPrompt}
        </p>
      </Section>

      {/* Saved configuration for launching sessions. */}
      <Collapsible className="mt-6">
        <h2 className="mb-2 text-sm font-medium text-muted-foreground">
          <CollapsibleTrigger className="group flex cursor-pointer items-center gap-1 rounded-sm text-left transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <ChevronDownIcon
              aria-hidden="true"
              className="size-4 transition-transform group-data-[state=closed]:-rotate-90"
            />
            Configuration
          </CollapsibleTrigger>
        </h2>
        <CollapsibleContent>
          <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <ConfigField label="Agent" value={agentLabel} testId="task-detail-agent" />
            <ConfigField label="Host" value={hostLabel} testId="task-detail-host" />
            <ConfigField
              label="Model"
              value={
                CLAUDE_NATIVE_MODELS.find((model) => model.id === task.modelOverride)?.label ??
                formatStatusModelLabel(task.modelOverride) ??
                "Agent default"
              }
              testId="task-detail-model"
            />
            <ConfigField
              label="Effort"
              value={formatStatusEffortLabel(task.reasoningEffort) ?? "Agent default"}
              testId="task-detail-effort"
            />
            <ConfigField
              label="Permission mode"
              value={permissionLabel}
              testId="task-detail-permission"
            />
            <ConfigField
              label="Session"
              value={
                task.sessionMode === "continue"
                  ? "Continue in the same session"
                  : "New session each run"
              }
              testId="task-detail-session-mode"
            />
            <ConfigField
              label="Workspace"
              value={
                task.workspace ??
                (task.executionTarget === "managed_sandbox"
                  ? "Sandbox default"
                  : "Host home directory")
              }
              testId="task-detail-workspace"
            />
            {/* The project is the owner's, so a viewer has no name for it. */}
            {!readOnly &&
              (task.projectId != null || (info !== "loading" && info.scheduled_task_projects)) && (
                <ConfigField
                  label="Project"
                  value={
                    projects?.find((project) => project.id === task.projectId)?.name ??
                    task.projectId ??
                    "No project"
                  }
                  testId="task-detail-project"
                />
              )}
            <ConfigField label="Timezone" value={task.timezone} testId="task-detail-timezone" />
          </dl>
          {task.sessionMode === "continue" && task.sessionId && (
            <p className="mt-3 text-xs text-muted-foreground">
              Model, effort, and permission mode are the saved launch settings. The continued
              session may have changed settings since it started.
            </p>
          )}
        </CollapsibleContent>
      </Collapsible>

      <hr className="my-6 border-border" />

      {/* Runs. */}
      <h2 className="mb-2 text-sm font-medium text-muted-foreground">Runs</h2>
      {runs?.some((run) => run.sessionCostUsd != null) && (
        <p className="mb-2 text-xs text-muted-foreground">
          Session cost is each session’s current recorded total. Runs sharing a session show the
          same total. Separate sub-agent sessions are excluded.
        </p>
      )}
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
            <RunRow
              key={run.id}
              run={run}
              now={now}
              highlighted={run.id === highlightedRunId}
              steps={runStepsEnabled ? runSteps.get(run.id) : undefined}
            />
          ))}
        </ul>
      )}

      {!readOnly && (
        <CreateScheduledTaskDialog
          open={editOpen}
          onOpenChange={setEditOpen}
          editingTask={task}
          sessionLink={sessionLink}
        />
      )}
      {canShare && !sharingOff && (
        <PermissionsModal
          automation={task}
          open={shareOpen || shareRequested}
          onOpenChange={handleShareOpenChange}
        />
      )}
    </PageScroll>
  );
}

/** Shows an owner-only control disabled, with a tooltip saying why, to a viewer. */
function OwnerOnly({
  readOnly,
  label,
  reason,
  children,
}: {
  readOnly: boolean;
  label: string;
  reason: string;
  children: ReactNode;
}) {
  if (!readOnly) return children;
  return (
    <DisabledActionTooltip reason={reason} label={label}>
      {children}
    </DisabledActionTooltip>
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

/** Link to the session every run copies, named after it once its snapshot loads. */
function SourceSessionLink({ sessionId }: { sessionId: string }) {
  const { session } = useSession(sessionId);
  return (
    <Link
      to={`/c/${encodeURIComponent(sessionId)}`}
      className="text-sm text-primary underline underline-offset-2 hover:text-primary/80"
      data-testid="task-detail-starts-from"
    >
      {session?.title || "Source session"}
    </Link>
  );
}

function ConfigField({ label, value, testId }: { label: string; value: string; testId: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground uppercase tracking-wide">{label}</dt>
      <dd className="break-words text-sm text-foreground" data-testid={testId}>
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
 * row: no hover highlight, no pointer, not focusable.
 */
// Shown before a run's status text, since the words alone don't say how the work went.
const STATUS_TEXT_LABELS: Record<StepStatus, string> = {
  started: "Started: ",
  done: "Done: ",
  failed: "Failed: ",
  needs_input: `${WAITING_FOR_YOU}: `,
};

function RunRow({
  run,
  now,
  highlighted,
  steps,
}: {
  run: ScheduledTaskRun;
  now: Date;
  highlighted: boolean;
  /** The run's latest plan, independent of the automation's current prompt. */
  steps?: ScheduledTaskRunSteps;
}) {
  const timestamp = formatRunTimestamp(run.firedAt ?? run.scheduledAt, now);
  // A workflow run's tasks, as the workflow's page shows them.
  const working = useSessionWorking(
    steps?.isCurrent === true ? run.conversationId : null,
    run.status === "running" || run.conversationStatus === "running",
  );
  const sessionWorking = steps?.isCurrent === true && working;
  const progress = useMemo(
    () => (steps ? stepProgress(steps.steps, steps.statuses, sessionWorking) : []),
    [steps, sessionWorking],
  );
  // What the run's agent last wrote about its work, when the run wrote a plan.
  const statusText = useMemo(() => (steps ? latestStatusText(steps.statuses) : null), [steps]);
  const sessionId = run.conversationId;
  const showTasks = progress.length > 0 && sessionId != null;
  const [graphOpen, setGraphOpen] = useState(false);
  // The saved workflow the run's plan follows, when the Workflows page can open it.
  const workflowId = isFeatureEnabled(useServerInfo(), "workflows")
    ? (steps?.workflowId ?? null)
    : null;
  // Stop while the run's session works; play once it stopped with tasks left.
  const control =
    showTasks && steps?.isCurrent && steps.planId
      ? planRunControl(progress, steps.statuses, sessionWorking, steps.planGraphRevision)
      : null;
  // What the run's buttons and task menus refresh once its session takes them.
  const refreshKeys = [
    scheduledTaskRunsKey(run.scheduledTaskId),
    ["scheduled-task-run-steps", run.scheduledTaskId],
  ];
  // Its tasks' ⋯ menus, on the timeline's cards and the graph's nodes.
  const taskMenus = useRunTaskMenus(
    steps?.isCurrent ? sessionId : null,
    refreshKeys,
    steps?.planId ?? null,
  );
  // Room on the row's link for the buttons over its right end.
  const buttonPadding = ["", "pr-11", "pr-18", "pr-25"][
    1 + (workflowId ? 1 : 0) + (control ? 1 : 0)
  ];
  const duration = formatRunDuration(run.firedAt, run.finishedAt);
  // Unread only applies to a succeeded run that produced a conversation.
  const unread = useRunUnread(run);
  const failure = isUnsuccessfulRun(run) ? describeRunFailure(run.errorCode, run.status) : null;
  const showSessionActivity = steps === undefined || steps.isCurrent;
  const inFlightLabel =
    IN_FLIGHT_RUN_LABELS[run.status] ??
    (showSessionActivity && isSessionStillRunning(run) ? "Session running" : undefined);

  const body = (
    <>
      <span className="flex size-5 shrink-0 items-center justify-center">
        <RunStatusIcon run={run} unread={unread} showSessionActivity={showSessionActivity} />
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="text-sm font-normal">{timestamp ?? "—"}</span>
          {duration && (
            <span className="text-xs text-muted-foreground" data-testid="run-duration">
              {duration}
            </span>
          )}
          {run.sessionCostUsd != null && (
            <span className="text-xs text-muted-foreground" data-testid="run-cost">
              Session cost {formatSessionCostUsd(run.sessionCostUsd)}
            </span>
          )}
          {inFlightLabel && !(run.conversationPendingApprovals ?? 0) && (
            <span className="text-xs text-muted-foreground" data-testid="run-status-label">
              {inFlightLabel}
            </span>
          )}
        </span>
        {showSessionActivity &&
          (run.conversationPendingApprovals ?? 0) > 0 &&
          run.conversationId && (
            <span
              className="text-xs text-amber-600 dark:text-amber-400"
              role="status"
              data-testid="run-pending-approval"
            >
              Session needs approval · Open to review
            </span>
          )}
        {statusText && (
          <span
            className={cn(
              "min-w-0 truncate text-xs",
              statusText.status === "failed"
                ? "text-amber-700 dark:text-amber-400"
                : "text-muted-foreground",
            )}
            data-testid="run-status-text"
            data-run-status={statusText.status}
            title={statusText.text}
          >
            <span className="font-medium">{STATUS_TEXT_LABELS[statusText.status]}</span>
            {statusText.text}
          </span>
        )}
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
        {failure && run.errorDetail && (
          <span
            className="text-xs break-words text-muted-foreground [overflow-wrap:anywhere]"
            data-testid="run-failure-detail"
          >
            {run.errorDetail}
          </span>
        )}
      </span>
    </>
  );

  const commonClassName = cn(
    "flex gap-3 rounded-lg px-2 py-2",
    failure ? "items-start" : "items-center",
  );

  // Keep the animation on the stable row when a queued run gains a session link.
  return (
    <li
      data-testid="task-detail-run"
      data-run-status={run.status}
      data-run-unread={run.status === "succeeded" ? unread : undefined}
      className={cn(
        "relative rounded-lg",
        highlighted && "animate-message-highlight",
        // The row's timeline sits under its link, so the whole row lights up, once.
        showTasks && "transition-colors hover:bg-muted has-[a:focus-visible]:bg-muted",
      )}
      style={highlighted ? { animationDuration: `${RUN_HIGHLIGHT_DURATION_MS}ms` } : undefined}
    >
      {run.conversationId ? (
        <Link
          to={`/c/${run.conversationId}`}
          data-testid="run-open"
          className={cn(
            commonClassName,
            "cursor-pointer focus-visible:outline-none",
            showTasks ? buttonPadding : "transition-colors hover:bg-muted focus-visible:bg-muted",
          )}
        >
          {body}
        </Link>
      ) : (
        <div className={commonClassName}>{body}</div>
      )}
      {showTasks && (
        <>
          <RunTimeline
            sessionId={sessionId}
            progress={progress}
            menuActionFor={taskMenus}
            className="pr-3 pb-2.5 pl-10"
          />
          <div className="absolute top-1.5 right-2 z-10 flex items-center gap-0.5">
            {control && steps?.planId && (
              <RunPlanButton
                sessionId={sessionId}
                runId={steps.planId}
                planName={steps?.planName ?? null}
                control={control}
                refreshKeys={refreshKeys}
              />
            )}
            {workflowId && <RunWorkflowButton workflowId={workflowId} />}
            <RunGraphButton
              runName={`the ${timestamp ?? ""} run`}
              onClick={() => setGraphOpen(true)}
            />
          </div>
        </>
      )}
      {graphOpen && showTasks && steps && (
        <WorkflowGraphOverlay
          title={steps.planName ?? "Workflow"}
          subtitle={
            <>
              <span>{timestamp ?? "—"}</span>
              <span aria-hidden>·</span>
              <Link to={`/c/${sessionId}`} className="text-foreground hover:underline">
                Open session
              </Link>
            </>
          }
          steps={steps.steps}
          progress={progress}
          menuActionFor={taskMenus}
          onClose={() => setGraphOpen(false)}
        />
      )}
    </li>
  );
}

/**
 * The leading LEFT status icon for a run. Exactly one renders, by priority:
 *   1. failed / incomplete → amber warning triangle, tooltip = "Failed".
 *   2. skipped             → muted calendar-off icon, tooltip = "Skipped".
 *   3. starting / running  → spinning loader icon, tooltip = the status.
 *      succeeded, session still running → the same spinner, tooltip "Session still running".
 *   4. succeeded + UNREAD  → blue filled dot, tooltip = "Unread".
 *   5. succeeded + READ / no conversation → muted grey dot, tooltip = "Completed".
 *   6. scheduled           → muted grey dot, no tooltip (not started yet).
 */
function RunStatusIcon({
  run,
  unread,
  showSessionActivity,
}: {
  run: ScheduledTaskRun;
  unread: boolean;
  showSessionActivity: boolean;
}) {
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
  if (showSessionActivity && isSessionStillRunning(run)) {
    return (
      <IndicatorWithTooltip tooltip="Session still running" announce={false}>
        <Loader2Icon
          data-testid="run-status-icon"
          data-run-icon="session-running"
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

/**
 * A succeeded run whose session is still working. The run row settles when its
 * turn ends, but the session can keep running (background work, a manager
 * session that outlives the turn), so a "completed" dot would read as finished.
 */
function isSessionStillRunning(run: ScheduledTaskRun): boolean {
  return (
    run.status === "succeeded" &&
    run.conversationId !== null &&
    run.conversationStatus === "running"
  );
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
