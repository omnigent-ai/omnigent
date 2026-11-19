/**
 * Scheduled tasks page (`/tasks`) — the list of the user's recurring agent
 * tasks, a search + Active/Paused filter, a "New automation" button,
 * and tabs for existing automations and reusable templates.
 *
 * Data comes from `useScheduledTasks`. Pause/resume and delete go through the
 * update/delete mutations, which invalidate the list. Share opens the shared
 * permissions dialog for an automation the viewer owns. On a multi-user server
 * that lists them, a "Shared with me" tab lists other users' automations
 * shared with the viewer, read-only, a page at a time.
 * The human-readable schedule and next-run text are computed client-side from
 * each task's stored RRULE (`scheduleText`) — there is no backend next-run
 * endpoint.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ClockIcon,
  InfoIcon,
  Loader2Icon,
  PlusIcon,
  SearchIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { PermissionsModal } from "@/components/PermissionsModal";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { showToast } from "@/components/ui/toast";
import { useOmnigentAnalytics, useOmnigentPageView } from "@/lib/analytics";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { AutomationsIntro } from "@/components/scheduled/AutomationsIntro";
import { ScheduledTaskRow } from "@/components/scheduled/ScheduledTaskRow";
import { AUTOMATION_TEMPLATES } from "@/components/scheduled/automationTemplates";
import { SESSION_LINK_ID_PLACEHOLDER } from "@/components/scheduled/triggerTemplates";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled, isSharingOff, SHARING_OFF_REASON } from "@/lib/capabilities";
import { useNavigate, useRebasePath, useSearchParams } from "@/lib/routing";
import { getShareableSessionLink } from "@/lib/shareLink";
import {
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTasks,
  useSharedScheduledTasks,
  useUpdateScheduledTask,
} from "@/hooks/useScheduledTasks";
import { useNow } from "@/hooks/useNow";
import { useViewerId } from "@/hooks/useViewerId";
import { canShareAutomation, sharedAutomationsListed } from "@/lib/permissionsApi";
import { ScheduledTaskApiError, type ScheduledTask } from "@/lib/scheduledTasksApi";
import { nextRunAtMs } from "@/lib/scheduleText";
import { cn } from "@/lib/utils";

type FilterTab = "all" | "active" | "paused";

const FILTER_TABS: { value: FilterTab; label: string }[] = [
  { value: "all", label: "All" },
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
];

/**
 * The tasks matching the search and Active/Paused filter: ACTIVE first by soonest
 * next run, PAUSED last by name. The server's next run is authoritative (it counts
 * COUNT from the task's creation, which the client can't); the local estimate is
 * only a fallback. Active rows with no computable next run sort after the rest.
 */
function filterAndSortTasks(
  tasks: ScheduledTask[],
  search: string,
  filter: FilterTab,
): ScheduledTask[] {
  const q = search.trim().toLowerCase();
  const matches = tasks.filter((t) => {
    if (filter === "active" && t.state !== "active") return false;
    if (filter === "paused" && t.state !== "paused") return false;
    if (q && !t.name.toLowerCase().includes(q)) return false;
    return true;
  });
  const nextRunByTaskId = new Map(
    matches
      .filter((t) => t.state === "active")
      .map((t) => {
        const server = t.nextRunAt != null ? Date.parse(t.nextRunAt) : Number.NaN;
        return [t.id, Number.isNaN(server) ? nextRunAtMs(t.rrule, t.timezone) : server];
      }),
  );
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
}

export function TasksPage({ templatesOnly = false }: { templatesOnly?: boolean }) {
  const newTaskButtonRef = useRef<HTMLButtonElement>(null);
  const introButtonRef = useRef<HTMLButtonElement>(null);
  // The guide shows once by itself; each click on the info button reopens it.
  const [introOpens, setIntroOpens] = useState(0);
  const [pointerTabInteraction, setPointerTabInteraction] = useState(false);
  const { data: tasks, isLoading, isError, refetch } = useScheduledTasks();
  const { trackClick, trackValueChange } = useOmnigentAnalytics();
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
  // Kept after closing so the dialog can animate out.
  const [sharingTask, setSharingTask] = useState<ScheduledTask | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  const viewerId = useViewerId();
  const [templateId, setTemplateId] = useState<string | null>(null);
  const selectedTemplate = AUTOMATION_TEMPLATES.find((template) => template.id === templateId);
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const info = useServerInfo();
  const sharedListAvailable = sharedAutomationsListed(info);
  const requestedViewParam = searchParams.get("view");
  // A linked Shared tab waits for the server's capabilities rather than bouncing.
  const view =
    templatesOnly || requestedViewParam === "templates"
      ? "templates"
      : requestedViewParam === "shared" && (info === "loading" || sharedListAvailable)
        ? "shared"
        : "yours";
  useOmnigentPageView(
    view === "templates" ? "tasks.templates" : view === "shared" ? "tasks.shared" : "tasks",
  );
  const sharedQuery = useSharedScheduledTasks(sharedListAvailable && view === "shared");
  const requestedView = useRef<string>(view);
  useEffect(() => {
    requestedView.current = view;
  }, [view]);
  useEffect(() => {
    if (templatesOnly) navigate("/tasks?view=templates", { replace: true });
  }, [templatesOnly, navigate]);
  // A trigger's manager keeps one session and starts its workers from it, so
  // the templates need "continue in the same session" and session orchestration.
  const triggersSupported =
    info !== "loading" &&
    info.scheduled_task_session_modes === true &&
    isFeatureEnabled(info, "session_orchestration");
  // Workers' links are posted outside Omnigent, so they go through the same
  // share-link path as the Share dialog, host transform included.
  const rebasePath = useRebasePath();
  const templatesFallbackText =
    info === "loading"
      ? "Loading templates…"
      : "Automation templates are not available on this server. You can still create an automation from scratch.";
  const sessionLink = getShareableSessionLink(SESSION_LINK_ID_PLACEHOLDER, rebasePath);

  function openManual() {
    setTemplateId(null);
    setEditingTask(null);
    setManualOpen(true);
  }

  function openFromTemplate(id: string) {
    trackClick(`tasks.template.${id}`, "button");
    setTemplateId(id);
    setEditingTask(null);
    setManualOpen(true);
  }

  // After an automation is created, show the list with filters cleared so the
  // new row is visible (creating from a template starts on the gallery view).
  function handleCreated() {
    setFilter("all");
    setSearch("");
    if (view !== "yours") {
      requestedView.current = "yours";
      setSearchParams((current) => {
        const next = new URLSearchParams(current);
        next.delete("view");
        return next;
      });
    }
    showToast("Automation created");
  }

  function handleManualOpenChange(next: boolean) {
    setManualOpen(next);
    if (!next) {
      setTemplateId(null);
      setEditingTask(null);
    }
  }

  const filtered = useMemo(
    () => filterAndSortTasks(tasks ?? [], search, filter),
    [tasks, search, filter],
  );
  const sharedTasks = useMemo(
    () => sharedQuery.data?.pages.flatMap((page) => page.tasks) ?? [],
    [sharedQuery.data],
  );
  const sharedFiltered = useMemo(
    () => filterAndSortTasks(sharedTasks, search, filter),
    [sharedTasks, search, filter],
  );
  const {
    hasNextPage: sharedHasMore,
    isFetchingNextPage: sharedFetchingMore,
    isError: sharedFailed,
  } = sharedQuery;
  const fetchMoreShared = sharedQuery.fetchNextPage;
  // A page can come back empty while more remain; keep going until one has rows,
  // but not past a failure, which waits for Retry.
  useEffect(() => {
    if (
      view === "shared" &&
      sharedTasks.length === 0 &&
      sharedHasMore &&
      !sharedFetchingMore &&
      !sharedFailed
    ) {
      void fetchMoreShared();
    }
  }, [view, sharedTasks.length, sharedHasMore, sharedFetchingMore, sharedFailed, fetchMoreShared]);

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
    runNowMutation.mutate(task.id, {
      onSuccess: () => {
        showToast(
          "Run triggered, it will start shortly. Open automation details to check run history.",
        );
      },
      onError: (err) => {
        if (err instanceof ScheduledTaskApiError && err.status === 409) {
          showToast("This run is already in progress");
        } else {
          showToast("Couldn't start the run");
        }
      },
    });
  }

  function handleEdit(task: ScheduledTask) {
    setTemplateId(null);
    setEditingTask(task);
    setManualOpen(true);
  }

  function handleShare(task: ScheduledTask) {
    setSharingTask(task);
    setShareOpen(true);
  }

  const hasAnyTasks = (tasks ?? []).length > 0;
  const hasAnyShared = sharedTasks.length > 0;
  const listControls = (hasAny: boolean) => (
    <TaskListControls
      search={search}
      onSearchChange={setSearch}
      filter={filter}
      onFilterChange={(value) => {
        trackClick(`tasks.filter_${value}`, "button");
        setFilter(value);
      }}
      showFilters={hasAny}
    />
  );

  const createDialog = (
    <CreateScheduledTaskDialog
      open={manualOpen}
      onOpenChange={handleManualOpenChange}
      initialName={selectedTemplate?.prefill.name}
      initialPrompt={selectedTemplate?.prefill.prompt}
      initialTrigger={selectedTemplate?.trigger}
      initialTemplateId={selectedTemplate?.id}
      sessionLink={sessionLink}
      editingTask={editingTask}
      onCreated={handleCreated}
    />
  );

  return (
    <PageScroll
      maxWidthClassName="max-w-4xl"
      className="[scrollbar-gutter:stable]"
      contentClassName="px-4 md:px-8"
    >
      {tasks !== undefined && (view === "yours" || introOpens > 0) && (
        <AutomationsIntro
          openCount={introOpens}
          onClose={() => (introOpens > 0 ? introButtonRef : newTaskButtonRef).current?.focus()}
        />
      )}
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1">
            <h1 className="text-2xl font-medium">Automations</h1>
            <Button
              ref={introButtonRef}
              variant="ghost"
              size="icon-xs"
              aria-label="About automations"
              data-testid="tasks-intro-button"
              componentId="tasks.intro.open"
              className="text-muted-foreground"
              onClick={() => setIntroOpens((n) => n + 1)}
            >
              <InfoIcon className="size-4" />
            </Button>
          </div>
        </div>
        <Button
          ref={newTaskButtonRef}
          data-testid="new-task-button"
          className="shrink-0"
          componentId="tasks.new"
          onClick={openManual}
        >
          <PlusIcon />
          New
        </Button>
      </div>

      <Tabs
        value={view}
        onValueChange={(value) => {
          // Pointer and focus can select the same tab before navigation renders.
          if (requestedView.current === value) return;
          requestedView.current = value;
          trackValueChange("tasks.view", "tabs", value, { valueHasNoPii: true });
          setSearchParams((current) => {
            const next = new URLSearchParams(current);
            if (value === "templates" || value === "shared") next.set("view", value);
            else next.delete("view");
            return next;
          });
        }}
      >
        <TabsList
          variant="pill"
          aria-label="Automation views"
          // Programmatic tab focus can retain :focus-visible after a pointer click.
          onPointerDownCapture={() => setPointerTabInteraction(true)}
          onKeyDownCapture={() => setPointerTabInteraction(false)}
          onFocus={(event) => {
            // After a pointer click, Radix can leave keyboard re-entry on the container.
            if (event.target === event.currentTarget) {
              event.currentTarget
                .querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')
                ?.focus();
            }
          }}
          onBlurCapture={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setPointerTabInteraction(false);
          }}
          className={cn(
            "gap-1",
            pointerTabInteraction &&
              "[&_[role=tab]:focus-visible]:border-transparent [&_[role=tab]:focus-visible]:ring-0 [&_[role=tab]:focus-visible]:outline-none",
          )}
        >
          <TabsTrigger value="yours" className="h-8 flex-none px-3">
            Your automations
          </TabsTrigger>
          {(sharedListAvailable || view === "shared") && (
            <TabsTrigger value="shared" className="h-8 flex-none px-3">
              Shared with me
            </TabsTrigger>
          )}
          <TabsTrigger value="templates" className="h-8 flex-none px-3">
            Templates
          </TabsTrigger>
        </TabsList>
        <TabsContent value="yours" className="mt-4">
          {listControls(hasAnyTasks)}

          {isError ? (
            <div
              role="alert"
              data-testid="tasks-load-error"
              className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-ui"
            >
              <TriangleAlertIcon className="size-4 shrink-0 text-destructive" />
              <span className="flex-1">Couldn’t load automations.</span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void refetch()}
                componentId="tasks.retry"
              >
                Retry
              </Button>
            </div>
          ) : isLoading ? (
            <div className="flex items-center gap-2 py-12 text-ui text-muted-foreground">
              <Loader2Icon className="size-4 animate-spin" />
              Loading automations…
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState hasAny={hasAnyTasks} />
          ) : (
            <div className="flex flex-col gap-2" data-testid="tasks-list">
              {filtered.map((task) => (
                <ScheduledTaskRow
                  key={task.id}
                  task={task}
                  now={now}
                  busy={busyId === task.id}
                  onEdit={handleEdit}
                  onPauseToggle={handlePauseToggle}
                  onRunNow={handleRunNow}
                  onDelete={handleDelete}
                  onShare={
                    canShareAutomation(info, viewerId, task.ownerUserId) ? handleShare : undefined
                  }
                  shareDisabledReason={isSharingOff(info) ? SHARING_OFF_REASON : undefined}
                />
              ))}
            </div>
          )}
          {!isError && !isLoading && filter === "all" && !search.trim() && triggersSupported && (
            <TemplateGallery onPick={openFromTemplate} preview />
          )}
        </TabsContent>
        <TabsContent value="shared" className="mt-4">
          {listControls(hasAnyShared)}
          {sharedQuery.isError ? (
            <div
              role="alert"
              data-testid="shared-tasks-load-error"
              className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-ui"
            >
              <TriangleAlertIcon className="size-4 shrink-0 text-destructive" />
              <span className="flex-1">Couldn’t load automations shared with you.</span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void sharedQuery.refetch()}
                componentId="tasks.shared.retry"
              >
                Retry
              </Button>
            </div>
          ) : sharedQuery.isLoading || (!hasAnyShared && sharedHasMore) ? (
            <div className="flex items-center gap-2 py-12 text-ui text-muted-foreground">
              <Loader2Icon className="size-4 animate-spin" />
              Loading automations…
            </div>
          ) : sharedFiltered.length === 0 ? (
            <div className="py-8" data-testid="shared-tasks-empty-state">
              <div className="py-10 text-center text-ui text-muted-foreground">
                {hasAnyShared ? "No automations found" : "No automations are shared with you yet"}
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-2" data-testid="shared-tasks-list">
              {sharedFiltered.map((task) => (
                <ScheduledTaskRow
                  key={task.id}
                  task={task}
                  now={now}
                  busy={false}
                  onEdit={handleEdit}
                  onPauseToggle={handlePauseToggle}
                  onRunNow={handleRunNow}
                  onDelete={handleDelete}
                />
              ))}
            </div>
          )}
          {!sharedQuery.isError && hasAnyShared && sharedHasMore && (
            <div className="mt-3 flex justify-center">
              <Button
                variant="outline"
                size="sm"
                data-testid="shared-tasks-show-more"
                componentId="tasks.shared.show_more"
                disabled={sharedFetchingMore}
                onClick={() => void fetchMoreShared()}
              >
                {sharedFetchingMore && <Loader2Icon className="size-4 animate-spin" />}
                Show more
              </Button>
            </div>
          )}
        </TabsContent>
        <TabsContent value="templates" className="mt-4">
          {triggersSupported ? (
            <TemplateGallery onPick={openFromTemplate} />
          ) : (
            <p className="py-8 text-ui text-muted-foreground">{templatesFallbackText}</p>
          )}
        </TabsContent>
      </Tabs>

      {createDialog}
      {sharingTask && (
        <PermissionsModal
          key={sharingTask.id}
          automation={sharingTask}
          open={shareOpen}
          onOpenChange={setShareOpen}
        />
      )}
    </PageScroll>
  );
}

/** The search box and, once there are tasks, the All / Active / Paused filter. */
function TaskListControls({
  search,
  onSearchChange,
  filter,
  onFilterChange,
  showFilters,
}: {
  search: string;
  onSearchChange: (value: string) => void;
  filter: FilterTab;
  onFilterChange: (value: FilterTab) => void;
  showFilters: boolean;
}) {
  return (
    <div className="mb-4 flex flex-col gap-5">
      <div className="relative">
        <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search automations…"
          componentId="tasks.search"
          data-testid="tasks-search"
          className="pl-9"
        />
      </div>
      {showFilters && (
        <div aria-label="Filter automations" className="flex items-center gap-1">
          {FILTER_TABS.map((tab) => (
            <button
              key={tab.value}
              type="button"
              aria-pressed={filter === tab.value}
              data-testid={`tasks-filter-${tab.value}`}
              onClick={() => onFilterChange(tab.value)}
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
    </div>
  );
}

function EmptyState({ hasAny }: { hasAny: boolean }) {
  return (
    <div className="py-8" data-testid="tasks-empty-state">
      {hasAny ? (
        <div className="py-10 text-center text-ui text-muted-foreground">No automations found</div>
      ) : (
        <div className="flex flex-col items-center gap-2 py-8 text-center">
          <ClockIcon className="size-8 text-muted-foreground/50" />
          <p className="text-ui font-medium">No automations yet</p>
          <p className="max-w-sm text-sm text-muted-foreground">
            Create an automation to run an agent session automatically on a recurring schedule.
          </p>
        </div>
      )}
    </div>
  );
}

function TemplateGallery({
  onPick,
  preview = false,
}: {
  onPick: (id: string) => void;
  preview?: boolean;
}) {
  return (
    <section
      className={preview ? "mt-6 border-t border-border/60 pt-6" : undefined}
      data-testid="tasks-suggestions"
      aria-label={preview ? "Suggested Automations" : "Automation templates"}
    >
      {preview && <h2 className="mb-3 text-base font-semibold">Suggested</h2>}
      <div className="grid gap-3 md:grid-cols-2">
        {AUTOMATION_TEMPLATES.map((template) => {
          const Icon = template.icon;
          const detailsId = `automation-template-${template.id}`;
          return (
            <button
              key={template.id}
              type="button"
              aria-label={template.title}
              aria-describedby={`${detailsId}-description ${detailsId}-requirements`}
              onClick={() => onPick(template.id)}
              data-testid={template.testId}
              className="flex cursor-pointer items-start gap-4 rounded-[20px] border border-border bg-card p-4 text-left text-ui transition-colors hover:border-foreground/20 hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-muted">
                <Icon className={cn("size-5", template.iconClassName)} />
              </span>
              <span className="flex min-w-0 flex-col gap-2">
                <span className="text-ui font-medium">{template.title}</span>
                <span id={`${detailsId}-description`} className="text-sm text-muted-foreground">
                  {template.description}
                </span>
                <span
                  id={`${detailsId}-requirements`}
                  className="text-caption text-muted-foreground"
                >
                  {template.needs}
                </span>
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
