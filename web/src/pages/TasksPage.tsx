/**
 * Scheduled tasks page (`/tasks`) — the list of the user's recurring agent
 * tasks, a search + Active/Paused filter, a "New task" dropdown (Create with
 * Omnigent / Set up manually), and a static Suggestions section below.
 *
 * Data comes from `useScheduledTasks` (GET /v1/scheduled-tasks). Pause/resume
 * and delete go through the update/delete mutations, which invalidate the list.
 * The human-readable schedule and next-run text are computed client-side from
 * each task's stored RRULE (`scheduleText`) — there is no backend next-run
 * endpoint.
 */

import { useMemo, useState } from "react";
import {
  CalendarClockIcon,
  ChevronDownIcon,
  Loader2Icon,
  PlusIcon,
  SearchIcon,
  SparklesIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { CreateWithOmnigentDialog } from "@/components/scheduled/CreateWithOmnigentDialog";
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
  const [omnigentOpen, setOmnigentOpen] = useState(false);
  // Prefill for the manual create dialog when opened from a "More ideas" chip.
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

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">Scheduled tasks</h1>
          <p className="text-sm text-muted-foreground">
            Run agent sessions on a recurring schedule. Tasks fire on a connected host.
          </p>
        </div>
        {/* Only one create path is live right now ("Set up manually"), so the
            "New task" button opens that dialog directly rather than showing a
            one-item dropdown. TODO(UI-2): re-enable the "Create with Omnigent"
            entry point and restore the two-option dropdown (NewTaskMenu). */}
        <Button data-testid="new-task-button" className="shrink-0" onClick={openManual}>
          New task
        </Button>
      </div>

      {/* Search + filter tabs. `gap-5` gives the filter-tab row clear separation
          from the search input (was gap-3 — too tight). No "Mark all as read"
          control: there is no unread model for scheduled tasks in this build, so
          it would act on nothing. Restore it if run-result unread state lands. */}
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
        {/* Codex-style tabs: the ACTIVE tab keeps a subtle resting background
            pill + full-strength foreground — NO bold weight anywhere. Inactive
            tabs are plain muted text with a subtle hover highlight. The
            distinction is background + color, not font weight. */}
        <div role="tablist" aria-label="Filter tasks" className="flex items-center gap-1">
          {FILTER_TABS.map((tab) => (
            <button
              key={tab.value}
              type="button"
              role="tab"
              aria-selected={filter === tab.value}
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
        <EmptyState hasAny={(tasks ?? []).length > 0} onCreate={openManual} />
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
      {filter === "all" && <SuggestionsSection onPick={openFromSuggestion} />}

      <CreateScheduledTaskDialog
        open={manualOpen}
        onOpenChange={handleManualOpenChange}
        initialName={prefill?.name}
        initialPrompt={prefill?.prompt}
      />
      {/* TODO(UI-2): stub kept wired but currently unreachable — nothing opens
          it while the "Create with Omnigent" menu entry is hidden. Restore the
          NewTaskMenu entry point (which calls setOmnigentOpen(true)) in UI-2. */}
      <CreateWithOmnigentDialog open={omnigentOpen} onOpenChange={setOmnigentOpen} />
    </PageScroll>
  );
}

/**
 * The "New task" split button: a caret dropdown with the two create paths.
 *
 * TODO(UI-2): currently NOT rendered — while "Create with Omnigent" is hidden,
 * the "New task" button opens the Set-up-manually dialog directly (see the
 * header above). Restore this dropdown (and the `onCreateWithOmnigent` wiring)
 * when the Omnigent create flow lands in UI-2.
 *
 * Exported (rather than deleted) so it's retained for UI-2 and doesn't trip
 * `noUnusedLocals` while it has no in-module caller.
 */
export function NewTaskMenu({
  onCreateWithOmnigent,
  onSetUpManually,
}: {
  onCreateWithOmnigent: () => void;
  onSetUpManually: () => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button data-testid="new-task-button" className="shrink-0 gap-1.5">
          <PlusIcon className="size-4" />
          New task
          <ChevronDownIcon className="size-3.5 opacity-70" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem
          onSelect={onCreateWithOmnigent}
          data-testid="new-task-omnigent"
          className="gap-2 py-2"
        >
          <SparklesIcon className="size-4 text-primary" />
          <div className="flex flex-col">
            <span className="text-sm">Create with Omnigent</span>
            <span className="text-[11px] text-muted-foreground">Describe it in plain language</span>
          </div>
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={onSetUpManually}
          data-testid="new-task-manual"
          className="gap-2 py-2"
        >
          <CalendarClockIcon className="size-4 text-muted-foreground" />
          <div className="flex flex-col">
            <span className="text-sm">Set up manually</span>
            <span className="text-[11px] text-muted-foreground">Fill in the schedule yourself</span>
          </div>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function EmptyState({ hasAny, onCreate }: { hasAny: boolean; onCreate: () => void }) {
  return (
    <div
      className="flex flex-col items-center gap-2 py-16 text-center"
      data-testid="tasks-empty-state"
    >
      <CalendarClockIcon className="size-8 text-muted-foreground/50" />
      <p className="text-sm font-medium">
        {hasAny ? "No tasks match your filters" : "No scheduled tasks yet"}
      </p>
      <p className="max-w-sm text-xs text-muted-foreground">
        {hasAny
          ? "Try a different search or filter."
          : "Create a task to run an agent session automatically on a recurring schedule."}
      </p>
      {!hasAny && (
        <Button size="sm" className="mt-2 gap-1.5" onClick={onCreate}>
          <PlusIcon className="size-4" />
          New task
        </Button>
      )}
    </div>
  );
}

/** Static suggestions rendered below the list. See `suggestions.ts` for the TODO. */
function SuggestionsSection({ onPick }: { onPick: (s: ScheduledTaskSuggestion) => void }) {
  return (
    // The single divider on the page: a `border-t` separating the task list
    // from the section. `mt-4 pt-4` keeps the gap tight.
    <div className="mt-4 border-t border-border/60 pt-4" data-testid="tasks-suggestions">
      {/* Muted section label, sized as a proper section header (text-sm) — a
          step up from text-xs while staying secondary in color. */}
      <h2 className="mb-2 text-sm font-medium text-muted-foreground">More ideas</h2>
      {/* Compact chips that wrap onto multiple lines. Each chip is an icon +
          TITLE only (no description) — a bordered, hoverable pill. Clicking a
          chip prefills/creates the task, same as the old row. Icons are neutral
          (muted) inside the neutral chip, matching the reference. */}
      <div className="flex flex-wrap gap-2">
        {SCHEDULED_TASK_SUGGESTIONS.map((s) => {
          const Icon = s.icon;
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => onPick(s)}
              data-testid={`suggestion-${s.id}`}
              className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted hover:text-foreground"
            >
              <Icon className="size-4 shrink-0 text-muted-foreground" />
              <span className="truncate">{s.title}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
