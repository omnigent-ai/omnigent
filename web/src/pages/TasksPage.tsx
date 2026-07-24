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

import { useMemo, useState } from "react";
import { ClockIcon, Loader2Icon, SearchIcon, TriangleAlertIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { ScheduledTaskRow } from "@/components/scheduled/ScheduledTaskRow";
import {
  SCHEDULED_TASK_SUGGESTIONS,
  type ScheduledTaskSuggestion,
} from "@/components/scheduled/suggestions";
import {
  useDeleteScheduledTask,
  useScheduledTasks,
  useUpdateScheduledTask,
} from "@/hooks/useScheduledTasks";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";
import { nextRunAtMs } from "@/lib/scheduleText";
import { cn } from "@/lib/utils";

type FilterTab = "all" | "active" | "paused";

const FILTER_TABS: { value: FilterTab; label: string }[] = [
  { value: "all", label: "All" },
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
];

export function TasksPage() {
  const { data: tasks, isLoading, isError, refetch } = useScheduledTasks();
  const updateMutation = useUpdateScheduledTask();
  const deleteMutation = useDeleteScheduledTask();

  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<FilterTab>("all");
  const [manualOpen, setManualOpen] = useState(false);
  // Prefill for the manual create dialog when opened from a "Suggestions" chip.
  // Null → the normal manual path (empty fields). Cleared on dialog close so a
  // stale prefill never leaks into a subsequent plain "New task" open.
  const [prefill, setPrefill] = useState<ScheduledTaskSuggestion["prefill"] | null>(null);

  function openManual() {
    setPrefill(null);
    setManualOpen(true);
  }

  function openFromSuggestion(s: ScheduledTaskSuggestion) {
    setPrefill(s.prefill);
    setManualOpen(true);
  }

  function handleManualOpenChange(next: boolean) {
    setManualOpen(next);
    if (!next) setPrefill(null);
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
    // Sort: ACTIVE first (soonest next-run at the top), PAUSED last. The
    // least-actionable (paused) rows sink to the bottom rather than leading the
    // list. Active rows with no computable next-run sort after those that have
    // one; paused rows keep a stable name order among themselves.
    return matches.slice().sort((a, b) => {
      const aPaused = a.state === "paused";
      const bPaused = b.state === "paused";
      if (aPaused !== bPaused) return aPaused ? 1 : -1;
      if (aPaused && bPaused) return a.name.localeCompare(b.name);
      const aNext = nextRunAtMs(a.rrule, a.timezone);
      const bNext = nextRunAtMs(b.rrule, b.timezone);
      if (aNext == null && bNext == null) return a.name.localeCompare(b.name);
      if (aNext == null) return 1;
      if (bNext == null) return -1;
      return aNext - bNext;
    });
  }, [tasks, search, filter]);

  // A per-task busy flag so a row's menu disables while its own mutation runs.
  const busyId =
    updateMutation.isPending && updateMutation.variables
      ? updateMutation.variables.id
      : deleteMutation.isPending
        ? (deleteMutation.variables as string | undefined)
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

  const hasAnyTasks = (tasks ?? []).length > 0;

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">Scheduled tasks</h1>
          <p className="text-sm text-muted-foreground">
            Run agent sessions on a recurring schedule. Tasks fire on a connected host.
          </p>
        </div>
        <Button data-testid="new-task-button" className="shrink-0" onClick={openManual}>
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
            placeholder="Search scheduled tasks…"
            data-testid="tasks-search"
            className="pl-9"
          />
        </div>
        {hasAnyTasks && (
          <div aria-label="Filter tasks" className="flex items-center gap-1">
            {FILTER_TABS.map((tab) => (
              <button
                key={tab.value}
                type="button"
                aria-pressed={filter === tab.value}
                data-testid={`tasks-filter-${tab.value}`}
                onClick={() => setFilter(tab.value)}
                className={cn(
                  "rounded-md px-3 py-1 text-sm font-medium transition-colors",
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
      </div>

      {isError ? (
        <div
          role="alert"
          data-testid="tasks-load-error"
          className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm"
        >
          <TriangleAlertIcon className="size-4 shrink-0 text-destructive" />
          <span className="flex-1">Couldn’t load scheduled tasks.</span>
          <Button variant="outline" size="sm" onClick={() => void refetch()}>
            Retry
          </Button>
        </div>
      ) : isLoading ? (
        <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading scheduled tasks…
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState
          hasAny={hasAnyTasks}
          showSuggestions={filter === "all"}
          onPickSuggestion={openFromSuggestion}
        />
      ) : (
        // Flat list — no boxed cards, and NO per-row hairline dividers (the row
        // padding alone gives the spacing). The only divider on the page is the
        // one before the Suggestions section (see SuggestionsSection).
        <div className="flex flex-col" data-testid="tasks-list">
          {filtered.map((task) => (
            <ScheduledTaskRow
              key={task.id}
              task={task}
              busy={busyId === task.id}
              onPauseToggle={handlePauseToggle}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}

      {/* Suggestions show ONLY on the "All" tab and are hidden once a specific
          filter ("Active" / "Paused") is selected, per the product spec ("when
          all isn't selected, suggestions should disappear"). Driven by the same
          `filter` state as the list, so switching tabs toggles it live. */}
      {filter === "all" && filtered.length > 0 && (
        <SuggestionsSection onPick={openFromSuggestion} />
      )}

      <CreateScheduledTaskDialog
        open={manualOpen}
        onOpenChange={handleManualOpenChange}
        initialName={prefill?.name}
        initialPrompt={prefill?.prompt}
      />
    </PageScroll>
  );
}

function EmptyState({
  hasAny,
  showSuggestions,
  onPickSuggestion,
}: {
  hasAny: boolean;
  showSuggestions: boolean;
  onPickSuggestion: (s: ScheduledTaskSuggestion) => void;
}) {
  return (
    <div className="py-8" data-testid="tasks-empty-state">
      {hasAny && (
        <div className="py-10 text-center text-sm text-muted-foreground">
          No scheduled tasks found
        </div>
      )}
      {!hasAny && (
        <div className="flex flex-col items-center gap-2 py-12 text-center">
          <ClockIcon className="size-8 text-muted-foreground/50" />
          <p className="text-sm font-medium">No scheduled tasks yet</p>
          <p className="max-w-sm text-xs text-muted-foreground">
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
      {showHeading && <h2 className="mb-3 text-sm text-muted-foreground">Suggestions</h2>}
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
              className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-1.5 text-sm font-normal transition-colors hover:bg-muted hover:text-foreground"
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
