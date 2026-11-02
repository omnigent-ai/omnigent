/**
 * Scheduled tasks page (`/tasks`) — the list of the user's recurring agent
 * tasks, a search + Active/Paused filter, a "New automation" button,
 * and tabs for existing automations and reusable templates.
 *
 * Data comes from `useScheduledTasks`. Pause/resume and delete go through the
 * update/delete mutations, which invalidate the list.
 * The human-readable schedule and next-run text are computed client-side from
 * each task's stored RRULE (`scheduleText`) — there is no backend next-run
 * endpoint.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { ClockIcon, InfoIcon, Loader2Icon, SearchIcon, TriangleAlertIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { AutomationsIntro } from "@/components/scheduled/AutomationsIntro";
import { ScheduledTaskRow } from "@/components/scheduled/ScheduledTaskRow";
import { AUTOMATION_TEMPLATES } from "@/components/scheduled/automationTemplates";
import { SESSION_LINK_ID_PLACEHOLDER } from "@/components/scheduled/triggerTemplates";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useNavigate, useRebasePath, useSearchParams } from "@/lib/routing";
import { getShareableSessionLink } from "@/lib/shareLink";
import {
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTasks,
  useUpdateScheduledTask,
} from "@/hooks/useScheduledTasks";
import { useNow } from "@/hooks/useNow";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";
import { nextRunAtMs } from "@/lib/scheduleText";
import { cn } from "@/lib/utils";

type FilterTab = "all" | "active" | "paused";

const FILTER_TABS: { value: FilterTab; label: string }[] = [
  { value: "all", label: "All" },
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
];

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
  const [templateId, setTemplateId] = useState<string | null>(null);
  const selectedTemplate = AUTOMATION_TEMPLATES.find((template) => template.id === templateId);
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const view = templatesOnly || searchParams.get("view") === "templates" ? "templates" : "yours";
  const requestedView = useRef<string>(view);
  useEffect(() => {
    requestedView.current = view;
  }, [view]);
  useEffect(() => {
    if (templatesOnly) navigate("/tasks?view=templates", { replace: true });
  }, [templatesOnly, navigate]);
  // A trigger's manager keeps one session and starts its workers from it, so
  // the templates need "continue in the same session" and session orchestration.
  const info = useServerInfo();
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
    setTemplateId(id);
    setEditingTask(null);
    setManualOpen(true);
  }

  function handleManualOpenChange(next: boolean) {
    setManualOpen(next);
    if (!next) {
      setTemplateId(null);
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
    setTemplateId(null);
    setEditingTask(task);
    setManualOpen(true);
  }

  const hasAnyTasks = (tasks ?? []).length > 0;

  const createDialog = (
    <CreateScheduledTaskDialog
      open={manualOpen}
      onOpenChange={handleManualOpenChange}
      initialName={selectedTemplate?.prefill.name}
      initialPrompt={selectedTemplate?.prefill.prompt}
      initialTrigger={selectedTemplate?.trigger}
      sessionLink={sessionLink}
      editingTask={editingTask}
    />
  );

  return (
    <PageScroll className="[scrollbar-gutter:stable]" contentClassName="px-4 md:px-6">
      {tasks !== undefined && (view === "yours" || introOpens > 0) && (
        <AutomationsIntro
          openCount={introOpens}
          onClose={() => (introOpens > 0 ? introButtonRef : newTaskButtonRef).current?.focus()}
        />
      )}
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1">
            <h1 className="text-2xl font-semibold">Automations</h1>
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
          <p className="text-ui text-muted-foreground">
            Run agent sessions on a recurring schedule.
          </p>
        </div>
        <Button
          ref={newTaskButtonRef}
          data-testid="new-task-button"
          className="shrink-0"
          componentId="tasks.new"
          onClick={openManual}
        >
          New automation
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
            if (value === "templates") next.set("view", "templates");
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
          <TabsTrigger value="templates" className="h-8 flex-none px-3">
            Templates
          </TabsTrigger>
        </TabsList>
        <TabsContent value="yours" className="mt-4">
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
            {hasAnyTasks && (
              <div aria-label="Filter automations" className="flex items-center gap-1">
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
          </div>

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
            <EmptyState
              hasAny={hasAnyTasks}
              showSuggestions={filter === "all"}
              onPickTemplate={openFromTemplate}
              triggersSupported={triggersSupported}
            />
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
                />
              ))}
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
    </PageScroll>
  );
}

function EmptyState({
  hasAny,
  showSuggestions,
  onPickTemplate,
  triggersSupported,
}: {
  hasAny: boolean;
  showSuggestions: boolean;
  onPickTemplate: (id: string) => void;
  triggersSupported: boolean;
}) {
  return (
    <div className="py-8" data-testid="tasks-empty-state">
      {hasAny ? (
        <div className="py-10 text-center text-ui text-muted-foreground">No automations found</div>
      ) : (
        <>
          <div className="flex flex-col items-center gap-2 py-8 text-center">
            <ClockIcon className="size-8 text-muted-foreground/50" />
            <p className="text-ui font-medium">No automations yet</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              Create an automation to run an agent session automatically on a recurring schedule.
            </p>
          </div>
          {showSuggestions && triggersSupported && (
            <TemplateGallery onPick={onPickTemplate} preview />
          )}
        </>
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
      aria-label={preview ? "Start from a template" : "Automation templates"}
    >
      {preview && <h2 className="mb-3 text-base font-semibold">Start from a template</h2>}
      <div className="grid gap-x-6 gap-y-2 md:grid-cols-2">
        {AUTOMATION_TEMPLATES.map((template) => {
          const Icon = template.icon;
          const detailsId = `automation-template-${template.id}`;
          return (
            <button
              key={template.id}
              type="button"
              aria-label={template.title}
              aria-describedby={`${detailsId}-description ${detailsId}-schedule ${detailsId}-requirements`}
              onClick={() => onPick(template.id)}
              data-testid={template.testId}
              className="flex cursor-pointer items-start gap-4 rounded-xl p-4 text-left text-ui transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-muted">
                <Icon className={cn("size-5", template.iconClassName)} />
              </span>
              <span className="flex min-w-0 flex-col gap-2">
                <span className="font-medium">{template.title}</span>
                <span id={`${detailsId}-description`} className="text-muted-foreground">
                  {template.description}
                </span>
                <span
                  id={`${detailsId}-schedule`}
                  className="flex items-center gap-2 text-muted-foreground"
                >
                  <ClockIcon className="size-4 shrink-0" />
                  Checks every hour
                </span>
                <span id={`${detailsId}-requirements`} className="text-xs text-muted-foreground">
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
