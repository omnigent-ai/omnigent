/**
 * Scheduled tasks page (`/tasks`) — the list of the user's recurring agent
 * tasks, a search + Active/Paused filter, a "New task" manual-create action,
 * and a static Suggestions section below.
 *
 * Data comes from `useScheduledTasks`. Pause/resume and delete go through the
 * update/delete mutations, which invalidate the list.
 * The human-readable schedule and next-run text are computed client-side from
 * each task's stored RRULE (`scheduleText`) — there is no backend next-run
 * endpoint.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { ClockIcon, Loader2Icon, SearchIcon, TriangleAlertIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { ProjectLabel } from "@/components/ProjectLabel";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { ScheduledTaskRow } from "@/components/scheduled/ScheduledTaskRow";
import {
  SCHEDULED_TASK_SUGGESTIONS,
  type ScheduledTaskSuggestion,
} from "@/components/scheduled/suggestions";
import {
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTasks,
  useUpdateScheduledTask,
} from "@/hooks/useScheduledTasks";
import { useNow } from "@/hooks/useNow";
import { useProjects } from "@/hooks/useConversations";
import {
  ScheduledTaskApiError,
  type ScheduledTask,
  type ScheduledTaskProjectFilter,
} from "@/lib/scheduledTasksApi";
import { nextRunAtMs } from "@/lib/scheduleText";
import { cn } from "@/lib/utils";

type FilterTab = "all" | "active" | "paused";

const FILTER_TABS: { value: FilterTab; label: string }[] = [
  { value: "all", label: "All" },
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
];

export function TasksPage() {
  const [projectFilterValue, setProjectFilterValue] = useState("all");
  const projectFilter = useMemo<ScheduledTaskProjectFilter>(() => {
    if (projectFilterValue === "unfiled") return { kind: "unfiled" };
    if (projectFilterValue.startsWith("project:")) {
      const projectId = projectFilterValue.slice("project:".length);
      if (projectId) return { kind: "project", projectId };
    }
    return { kind: "all" };
  }, [projectFilterValue]);
  const taskQuery = useScheduledTasks(projectFilter);
  const lastAllTasks = useRef<ScheduledTask[] | undefined>(undefined);
  if (projectFilter.kind === "all" && taskQuery.data !== undefined) {
    lastAllTasks.current = taskQuery.data;
  }
  const refetchAllAfterProjectReset = useRef(false);
  const {
    data: projects,
    isLoading: projectsLoading,
    isError: projectsError,
    refetch: refetchProjects,
  } = useProjects();
  const assignableProjects = useMemo(
    () => (projects ?? []).filter((project) => project.id !== null),
    [projects],
  );
  // Keyed by id to the WHOLE summary (not just the name) so consumers can
  // render the project's emoji icon alongside its name.
  const projectsById = useMemo(
    () => new Map(assignableProjects.map((project) => [project.id as string, project])),
    [assignableProjects],
  );
  const tasks = taskQuery.data;
  const { trackClick } = useOmnigentAnalytics();
  // A single shared, slowly-ticking clock for the whole list. Passing it down to
  // each row (rather than each row owning a timer) keeps the relative next-run
  // labels fresh with ONE interval regardless of how many rows are on screen.
  const now = useNow();
  const updateMutation = useUpdateScheduledTask();
  const deleteMutation = useDeleteScheduledTask();
  const runNowMutation = useRunScheduledTaskNow();

  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<FilterTab>("all");
  const [manualOpen, setManualOpen] = useState(false);
  const [editingTask, setEditingTask] = useState<ScheduledTask | null>(null);
  // Prefill for the manual create dialog when opened from a "Suggestions" chip.
  // Null → the normal manual path (empty fields). Cleared on dialog close so a
  // stale prefill never leaks into a subsequent plain "New task" open.
  const [prefill, setPrefill] = useState<ScheduledTaskSuggestion["prefill"] | null>(null);

  useEffect(() => {
    if (projectFilter.kind !== "project" || projects === undefined || projectsError) return;
    if (!assignableProjects.some((project) => project.id === projectFilter.projectId)) {
      setProjectFilterValue("all");
    }
  }, [assignableProjects, projectFilter, projects, projectsError]);

  useEffect(() => {
    if (
      projectFilter.kind === "project" &&
      taskQuery.error instanceof ScheduledTaskApiError &&
      taskQuery.error.status === 404
    ) {
      refetchAllAfterProjectReset.current = true;
      setProjectFilterValue("all");
    }
  }, [projectFilter, taskQuery.error]);

  useEffect(() => {
    if (projectFilter.kind !== "all" || !refetchAllAfterProjectReset.current) return;
    refetchAllAfterProjectReset.current = false;
    void taskQuery.refetch();
  }, [projectFilter.kind, taskQuery]);

  function openManual() {
    setPrefill(null);
    setEditingTask(null);
    setManualOpen(true);
  }

  function openFromSuggestion(s: ScheduledTaskSuggestion) {
    setPrefill(s.prefill);
    setEditingTask(null);
    setManualOpen(true);
  }

  function handleManualOpenChange(next: boolean) {
    setManualOpen(next);
    if (!next) {
      setPrefill(null);
      setEditingTask(null);
    }
  }

  const filtered = useMemo(() => {
    const all = tasks ?? [];
    const q = search.trim().toLowerCase();
    const matches = all.filter((t) => {
      if (filter === "active" && t.state !== "active") return false;
      if (filter === "paused" && t.state !== "paused") return false;
      if (q && !t.name.toLowerCase().includes(q)) return false;
      return true;
    });
    const nextRunByTaskId = new Map(
      matches
        .filter((t) => t.state === "active")
        .map((t) => [t.id, nextRunAtMs(t.rrule, t.timezone)]),
    );
    // Sort: ACTIVE first (soonest next-run at the top), PAUSED last. The
    // least-actionable (paused) rows sink to the bottom rather than leading the
    // list. Active rows with no computable next-run sort after those that have
    // one; paused rows keep a stable name order among themselves.
    return matches.slice().sort((a, b) => {
      const aPaused = a.state === "paused";
      const bPaused = b.state === "paused";
      if (aPaused !== bPaused) return aPaused ? 1 : -1;
      if (aPaused && bPaused) return a.name.localeCompare(b.name);
      const aNext = nextRunByTaskId.get(a.id) ?? null;
      const bNext = nextRunByTaskId.get(b.id) ?? null;
      if (aNext == null && bNext == null) return a.name.localeCompare(b.name);
      if (aNext == null) return 1;
      if (bNext == null) return -1;
      return aNext - bNext;
    });
  }, [tasks, search, filter]);

  // A per-task busy flag so a row's menu disables while its own mutation runs.
  // Covers pause/resume (update), delete, and run-now so the ⋯ menu can't be
  // re-triggered mid-flight for the task whose mutation is pending.
  const busyId =
    updateMutation.isPending && updateMutation.variables
      ? updateMutation.variables.id
      : deleteMutation.isPending
        ? (deleteMutation.variables as string | undefined)
        : runNowMutation.isPending
          ? (runNowMutation.variables as string | undefined)
          : undefined;

  function handlePauseToggle(task: ScheduledTask) {
    updateMutation.mutate({
      id: task.id,
      input: { state: task.state === "paused" ? "active" : "paused" },
    });
  }

  function handleDelete(task: ScheduledTask) {
    deleteMutation.mutate(task.id);
  }

  function handleRunNow(task: ScheduledTask) {
    runNowMutation.mutate(task.id);
  }

  function handleEdit(task: ScheduledTask) {
    setPrefill(null);
    setEditingTask(task);
    setManualOpen(true);
  }

  const rawSlice = tasks ?? [];
  const allTasks = projectFilter.kind === "all" ? taskQuery.data : lastAllTasks.current;
  const hasAnyGlobally = rawSlice.length > 0 || (allTasks ?? []).length > 0;
  const taskError = taskQuery.isError && taskQuery.data === undefined;
  const taskLoading = taskQuery.isLoading && taskQuery.data === undefined;
  const allSucceeded =
    projectFilter.kind === "all" &&
    !taskQuery.isError &&
    !taskQuery.isLoading &&
    taskQuery.data !== undefined;
  const showSuggestions =
    projectFilter.kind === "all" && filter === "all" && search.trim() === "" && allSucceeded;

  function retryTasks() {
    void taskQuery.refetch();
  }

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">Automations</h1>
          <p className="text-ui text-muted-foreground">
            Run agent sessions on a recurring schedule. Tasks fire on a connected host.
          </p>
        </div>
        <Button
          data-testid="new-task-button"
          className="shrink-0"
          onClick={openManual}
          componentId="tasks.new"
        >
          New task
        </Button>
      </div>

      {/* Search + filter tabs. No "Mark all as read" control: there is no unread
          model for scheduled tasks in this build, so it would act on nothing.
          Restore it if run-result unread state lands. */}
      <div className="mb-4 flex flex-col gap-5">
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search automations…"
            componentId="tasks.search"
            data-testid="tasks-search"
            className="pl-9"
          />
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {hasAnyGlobally && (
            <div aria-label="Filter tasks" className="flex items-center gap-1">
              {FILTER_TABS.map((tab) => (
                <button
                  key={tab.value}
                  type="button"
                  aria-pressed={filter === tab.value}
                  data-testid={`tasks-filter-${tab.value}`}
                  onClick={() => {
                    trackClick(`tasks.filter_${tab.value}`, "button");
                    setFilter(tab.value);
                  }}
                  className={cn(
                    "rounded-md px-3 py-1 text-ui font-medium transition-colors",
                    filter === tab.value
                      ? "bg-muted text-foreground"
                      : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                  )}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          )}
          <Select
            value={projectFilterValue}
            onValueChange={(value) => {
              const normalized =
                value === "all" ||
                value === "unfiled" ||
                (value.startsWith("project:") && value.slice("project:".length) !== "")
                  ? value
                  : "all";
              setProjectFilterValue(normalized);
            }}
          >
            <SelectTrigger
              data-testid="tasks-project-filter"
              className="min-w-40"
              disabled={projectsLoading}
            >
              {projectsLoading ? "Loading projects…" : <SelectValue />}
            </SelectTrigger>
            <SelectContent position="popper" align="start">
              <SelectItem value="all">All projects</SelectItem>
              <SelectItem value="unfiled">Unfiled</SelectItem>
              {assignableProjects.map((project) => (
                <SelectItem
                  key={project.id}
                  value={`project:${project.id}`}
                  disabled={projectsError}
                >
                  <ProjectLabel name={project.name} icon={project.icon} />
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {projectsError && (
            <p className="text-sm text-destructive">
              Couldn&apos;t load projects.{" "}
              <button type="button" className="underline" onClick={() => void refetchProjects()}>
                Retry
              </button>
            </p>
          )}
        </div>
      </div>

      {taskError ? (
        <div
          role="alert"
          data-testid="tasks-load-error"
          className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-ui"
        >
          <TriangleAlertIcon className="size-4 shrink-0 text-destructive" />
          <span className="flex-1">Couldn’t load automations.</span>
          <Button variant="outline" size="sm" onClick={retryTasks} componentId="tasks.retry">
            Retry
          </Button>
        </div>
      ) : taskLoading ? (
        <div className="flex items-center gap-2 py-12 text-ui text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading automations…
        </div>
      ) : rawSlice.length === 0 && allTasks !== undefined && !hasAnyGlobally ? (
        <EmptyState
          variant="global"
          message="No automations yet"
          showSuggestions={showSuggestions}
          onPickSuggestion={openFromSuggestion}
        />
      ) : rawSlice.length === 0 && projectFilter.kind === "project" ? (
        <EmptyState
          variant="narrowed"
          message={`No automations in ${projectsById.get(projectFilter.projectId)?.name ?? "this Project"}`}
          showSuggestions={false}
          onPickSuggestion={openFromSuggestion}
        />
      ) : rawSlice.length === 0 && projectFilter.kind === "unfiled" ? (
        <EmptyState
          variant="narrowed"
          message="No unfiled automations"
          showSuggestions={false}
          onPickSuggestion={openFromSuggestion}
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          variant="narrowed"
          message="No automations found"
          showSuggestions={false}
          onPickSuggestion={openFromSuggestion}
        />
      ) : (
        // Card list — each row is a bordered card (see ScheduledTaskRow), stacked
        // with a gap so there's vertical spacing between cards. The only divider
        // on the page is the one before the Suggestions section.
        <div className="flex flex-col gap-2" data-testid="tasks-list">
          {filtered.map((task) => (
            <ScheduledTaskRow
              key={task.id}
              task={task}
              project={
                projectFilter.kind === "all" && task.projectId
                  ? projectsById.get(task.projectId)
                  : undefined
              }
              now={now}
              busy={busyId === task.id}
              onEdit={handleEdit}
              onPauseToggle={handlePauseToggle}
              onRunNow={handleRunNow}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}

      {/* Suggestions show ONLY on the "All" tab and are hidden once a specific
          filter ("Active" / "Paused") is selected, per the product spec ("when
          all isn't selected, suggestions should disappear"). Driven by the same
          `filter` state as the list, so switching tabs toggles it live. */}
      {showSuggestions && filtered.length > 0 && <SuggestionsSection onPick={openFromSuggestion} />}

      <CreateScheduledTaskDialog
        open={manualOpen}
        onOpenChange={handleManualOpenChange}
        initialName={prefill?.name}
        initialPrompt={prefill?.prompt}
        initialProjectId={projectFilter.kind === "project" ? projectFilter.projectId : undefined}
        editingTask={editingTask}
      />
    </PageScroll>
  );
}

export function EmptyState({
  variant,
  message,
  showSuggestions,
  onPickSuggestion,
}: {
  variant: "global" | "narrowed";
  message: string;
  showSuggestions: boolean;
  onPickSuggestion: (s: ScheduledTaskSuggestion) => void;
}) {
  const globalEmpty = variant === "global";
  return (
    <div className="py-8" data-testid="tasks-empty-state">
      {globalEmpty ? (
        <div className="flex flex-col items-center gap-2 py-12 text-center">
          <ClockIcon className="size-8 text-muted-foreground/50" />
          <p className="text-ui font-medium">{message}</p>
          <p className="max-w-sm text-sm text-muted-foreground">
            Create a task to run an agent session automatically on a recurring schedule.
          </p>
          {showSuggestions && (
            <SuggestionsSection
              onPick={onPickSuggestion}
              showHeading={false}
              className="mt-3 border-t-0 pt-0"
            />
          )}
        </div>
      ) : (
        <div className="py-10 text-center text-ui text-muted-foreground">{message}</div>
      )}
    </div>
  );
}

/** Static suggestions rendered below the list. See `suggestions.ts` for the TODO. */
function SuggestionsSection({
  onPick,
  showHeading = true,
  className,
}: {
  onPick: (s: ScheduledTaskSuggestion) => void;
  showHeading?: boolean;
  className?: string;
}) {
  return (
    // The single divider on the page: a `border-t` separating the task list
    // from the section. `mt-4 pt-4` keeps the gap tight.
    <div
      className={cn("mt-4 border-t border-border/60 pt-4", className)}
      data-testid="tasks-suggestions"
    >
      {showHeading && <h2 className="mb-3 text-ui text-muted-foreground">Suggestions</h2>}
      {/* Compact chips that wrap onto multiple lines. */}
      <div className="flex flex-wrap gap-2">
        {SCHEDULED_TASK_SUGGESTIONS.map((s) => {
          const Icon = s.icon;
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => onPick(s)}
              data-testid={`suggestion-${s.id}`}
              className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-1.5 text-ui font-normal transition-colors hover:bg-muted hover:text-foreground"
            >
              <Icon className={cn("size-4 shrink-0", s.iconClassName)} />
              <span className="truncate">{s.title}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
