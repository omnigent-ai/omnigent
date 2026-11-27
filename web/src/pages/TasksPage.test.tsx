// Tests for /tasks: lists, filters, gallery tabs, and row actions.
// Scheduled-task hooks are mocked; the creation dialog exposes its seed props and close action.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setOmnigentHostConfig } from "@/lib/host";
import { TasksPage } from "./TasksPage";
import { useViewerId } from "@/hooks/useViewerId";
import * as hooks from "@/hooks/useScheduledTasks";
import { basenamedRouting, RoutingProvider } from "@/lib/routing";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { ScheduledTaskApiError, type ScheduledTask } from "@/lib/scheduledTasksApi";
import { showToast } from "@/components/ui/toast";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as AutomationListPaletteCommandsModule from "@/components/scheduled/automationListPaletteCommands";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

// Counts renders of the list's ⌘K commands, which render nothing themselves.
const listPaletteCommands = vi.hoisted(() => ({ renders: 0 }));
vi.mock("@/components/scheduled/automationListPaletteCommands", async (importOriginal) => {
  const original = await importOriginal<typeof AutomationListPaletteCommandsModule>();
  return {
    ...original,
    AutomationListPaletteCommands: (
      props: Parameters<typeof original.AutomationListPaletteCommands>[0],
    ) => {
      listPaletteCommands.renders += 1;
      return original.AutomationListPaletteCommands(props);
    },
  };
});

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: vi.fn() }));

vi.mock("@/hooks/useScheduledTasks", () => ({
  useScheduledTasks: vi.fn(),
  useSharedScheduledTasks: vi.fn(),
  useUpdateScheduledTask: vi.fn(),
  useDeleteScheduledTask: vi.fn(),
  useRunScheduledTaskNow: vi.fn(),
  useCloneScheduledTask: vi.fn(),
}));

// Stub the create dialog — its internals are covered separately; here we only
// need to know it opened and WHICH prefill (initialName/initialPrompt) TasksPage
// passed, so we can assert card selection seeds it and manual open does not.
vi.mock("@/components/scheduled/CreateScheduledTaskDialog", () => ({
  CreateScheduledTaskDialog: ({
    open,
    onOpenChange,
    initialName,
    initialPrompt,
    initialTrigger,
    initialTemplateId,
    initialSchedule,
    sessionLink,
    editingTask,
    onCreated,
  }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onCreated?: () => void;
    initialName?: string;
    initialPrompt?: string;
    initialTrigger?: string | null;
    initialTemplateId?: string;
    initialSchedule?: string;
    sessionLink?: string;
    editingTask?: ScheduledTask | null;
  }) =>
    open ? (
      <div
        data-testid="manual-dialog-open"
        data-initial-name={initialName ?? ""}
        data-initial-prompt={initialPrompt ?? ""}
        data-initial-trigger={initialTrigger ?? ""}
        data-initial-template={initialTemplateId ?? ""}
        data-initial-schedule={initialSchedule ?? ""}
        data-session-link={sessionLink ?? ""}
        data-editing-task-id={editingTask?.id ?? ""}
        data-editing-task-name={editingTask?.name ?? ""}
      >
        <button type="button" onClick={() => onOpenChange(false)}>
          Close setup
        </button>
        <button
          type="button"
          onClick={() => {
            onCreated?.();
            onOpenChange(false);
          }}
        >
          Create setup
        </button>
      </div>
    ) : null,
}));

// Stub the share dialog to a marker naming the automation it shares.
vi.mock("@/components/PermissionsModal", () => ({
  PermissionsModal: ({ automation, open }: { automation?: { id: string }; open: boolean }) =>
    open ? <div data-testid="share-dialog-open" data-automation-id={automation?.id ?? ""} /> : null,
}));

function task(overrides: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: "st_1",
    name: "Nightly triage",
    prompt: "Triage",
    rrule: "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0",
    ownerUserId: null,
    agentId: "ag_1",
    timezone: "UTC",
    createdAt: 1,
    updatedAt: 2,
    modelOverride: null,
    reasoningEffort: null,
    permissionMode: null,
    mcpServices: [],
    sessionMode: "new",
    sessionId: null,
    workspace: null,
    hostId: null,
    executionTarget: "connected_host",
    projectId: null,
    state: "active",
    lastRunAt: null,
    lastRunStatus: null,
    lastRunConversationId: null,
    nextRunAt: null,
    ...overrides,
  };
}

const mutate = vi.fn();
const deleteMutate = vi.fn();
const runNowMutate = vi.fn();
const cloneMutate = vi.fn();

function setTasks(
  tasks: ScheduledTask[] | undefined,
  state: { isLoading?: boolean; isError?: boolean } = {},
) {
  vi.mocked(hooks.useScheduledTasks).mockReturnValue({
    data: tasks,
    isLoading: state.isLoading ?? false,
    isError: state.isError ?? false,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof hooks.useScheduledTasks>);
}

const fetchNextSharedPage = vi.fn();

function setSharedPages(
  pages: { tasks: ScheduledTask[]; nextCursor: string | null }[] | undefined,
  state: { isLoading?: boolean; isError?: boolean; isFetchingNextPage?: boolean } = {},
) {
  vi.mocked(hooks.useSharedScheduledTasks).mockReturnValue({
    data: pages ? { pages, pageParams: pages.map(() => null) } : undefined,
    isLoading: state.isLoading ?? false,
    isError: state.isError ?? false,
    hasNextPage: (pages?.at(-1)?.nextCursor ?? null) !== null,
    isFetchingNextPage: state.isFetchingNextPage ?? false,
    fetchNextPage: fetchNextSharedPage,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof hooks.useSharedScheduledTasks>);
}

const analytics = vi.fn();
beforeEach(() => {
  fetchNextSharedPage.mockReset();
  setSharedPages(undefined);
  analytics.mockReset();
  setOmnigentHostConfig({ analytics });
  localStorage.clear();
  vi.mocked(useViewerId).mockReturnValue(null);
  mutate.mockReset();
  deleteMutate.mockReset();
  runNowMutate.mockReset();
  cloneMutate.mockReset();
  vi.mocked(showToast).mockReset();
  vi.mocked(hooks.useUpdateScheduledTask).mockReturnValue({
    mutate,
    isPending: false,
    variables: undefined,
  } as unknown as ReturnType<typeof hooks.useUpdateScheduledTask>);
  vi.mocked(hooks.useDeleteScheduledTask).mockReturnValue({
    mutate: deleteMutate,
    isPending: false,
    variables: undefined,
  } as unknown as ReturnType<typeof hooks.useDeleteScheduledTask>);
  vi.mocked(hooks.useRunScheduledTaskNow).mockReturnValue({
    mutate: runNowMutate,
    isPending: false,
    variables: undefined,
  } as unknown as ReturnType<typeof hooks.useRunScheduledTaskNow>);
  vi.mocked(hooks.useCloneScheduledTask).mockReturnValue({
    mutate: cloneMutate,
    isPending: false,
    variables: undefined,
  } as unknown as ReturnType<typeof hooks.useCloneScheduledTask>);
});

afterEach(() => {
  cleanup();
  setOmnigentHostConfig({});
});

const TRIGGER_INFO = {
  ...SERVER_INFO_OFFLINE_FALLBACK,
  scheduled_task_session_modes: true,
  features: { session_orchestration: true },
};

function renderPage(
  path = "/tasks",
  basename = "",
  info: typeof SERVER_INFO_OFFLINE_FALLBACK = TRIGGER_INFO,
) {
  return render(
    <CapabilitiesProvider info={info}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[`${basename}${path}`]}>
          <RoutingProvider value={basenamedRouting(basename)}>
            <Routes>
              <Route path={`${basename}/tasks`} element={<TasksPage />} />
              <Route
                path={`${basename}/tasks/templates`}
                element={<TasksPage key="templates" templatesOnly />}
              />
              <Route
                path={`${basename}/tasks/:taskId`}
                element={<div data-testid="task-detail-route" />}
              />
            </Routes>
          </RoutingProvider>
        </MemoryRouter>
      </TooltipProvider>
    </CapabilitiesProvider>,
  );
}

function selectView(name: "Your automations" | "Templates") {
  fireEvent.mouseDown(screen.getByRole("tab", { name }), { button: 0, ctrlKey: false });
}

describe("TasksPage list", () => {
  it("reopens the guide from the info button beside the heading", async () => {
    vi.mocked(useViewerId).mockReturnValue("alice");
    setTasks([]);
    render(
      <MemoryRouter>
        <CapabilitiesProvider info={SERVER_INFO_OFFLINE_FALLBACK}>
          <TasksPage />
        </CapabilitiesProvider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    await waitFor(() => expect(screen.getByTestId("new-task-button")).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "About automations" }));
    expect(screen.getByRole("dialog", { name: "Introducing Automations" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "See examples" }));
    expect(screen.getByRole("dialog", { name: "Example automations" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "About automations" })).toHaveFocus(),
    );
  });

  it("waits for the first task load and keeps the guide's step through a failed refetch", () => {
    vi.mocked(useViewerId).mockReturnValue("alice");
    const content = () => (
      <MemoryRouter>
        <CapabilitiesProvider info={SERVER_INFO_OFFLINE_FALLBACK}>
          <TasksPage />
        </CapabilitiesProvider>
      </MemoryRouter>
    );
    setTasks(undefined, { isLoading: true });
    const view = render(content());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    setTasks(undefined, { isError: true });
    view.rerender(content());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    setTasks([]);
    view.rerender(content());
    expect(screen.getByRole("dialog", { name: "Introducing Automations" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const step = screen.getByText(/of .* · Make it yours/).textContent;
    setTasks([], { isError: true });
    view.rerender(content());
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(screen.getByText(step!)).toBeVisible();
    setTasks([]);
    view.rerender(content());
    expect(screen.getByText(step!)).toBeVisible();
  });

  it("renders the title and task rows with schedule text", () => {
    setTasks([task()]);
    renderPage();
    expect(screen.getByText("Automations")).toBeInTheDocument();
    const row = screen.getByTestId("scheduled-task-row");
    expect(within(row).getByText("Nightly triage")).toBeInTheDocument();
    // Schedule text is derived client-side from the RRULE.
    expect(within(row).getByTestId("task-schedule-line").textContent).toContain(
      "Weekdays at 8:00 AM",
    );
  });

  it("paused rows: pill only — no dimming, no '· Paused' suffix, no status circle", () => {
    setTasks([task({ id: "st_2", name: "Paused one", state: "paused" })]);
    renderPage();
    const row = screen.getByTestId("scheduled-task-row");
    expect(row).toHaveAttribute("data-state", "paused");
    // NOT dimmed — the title must stay legible (AA). The pill is the sole signal.
    expect(row.className).not.toContain("opacity-60");
    // Small "Paused" pill next to the title.
    expect(within(row).getByTestId("task-paused-pill")).toBeInTheDocument();
    // No leading/trailing status circles — the ⋯ menu is the sole affordance
    // (hover-revealed, so no resume glyph on the row).
    expect(within(row).queryByTestId("task-resume-glyph")).toBeNull();
    // Subline is just the schedule — no "· Paused" suffix, no next-run clause.
    const line = within(row).getByTestId("task-schedule-line").textContent ?? "";
    expect(line).toContain("Weekdays at 8:00 AM");
    expect(line).not.toContain("Paused");
    expect(line).not.toContain("Next run");
  });

  it("resumes a paused task via the row menu (Resume label reflects state)", () => {
    setTasks([task({ id: "st_2", state: "paused" })]);
    renderPage();
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    // Paused → the toggle item reads "Resume".
    expect(screen.getByTestId("task-pause-toggle")).toHaveTextContent("Resume");
    fireEvent.click(screen.getByTestId("task-pause-toggle"));
    expect(mutate).toHaveBeenCalledWith({ id: "st_2", input: { state: "active" } });
  });

  it("shows the empty state when there are no tasks", () => {
    setTasks([]);
    renderPage();
    expect(screen.getByTestId("tasks-empty-state")).toBeInTheDocument();
    expect(screen.getByText("No automations yet")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Create an automation to run an agent session automatically on a recurring schedule.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("tasks-filter-all")).toBeNull();
    expect(screen.queryByTestId("tasks-filter-active")).toBeNull();
    expect(screen.queryByTestId("tasks-filter-paused")).toBeNull();
    expect(screen.queryAllByTestId("new-task-button")).toHaveLength(1);
  });

  it("shows descriptive template cards in the true empty state", () => {
    setTasks([]);
    renderPage();
    const suggestions = screen.getByTestId("tasks-suggestions");
    expect(within(suggestions).getByText("Suggested")).toBeInTheDocument();
    const cards = within(suggestions).getAllByTestId(/^suggestion-/);
    expect(cards).toHaveLength(5);
    expect(cards.map((r) => r.getAttribute("data-testid"))).toEqual([
      "suggestion-trigger-jira",
      "suggestion-trigger-slack",
      "suggestion-trigger-github",
      "suggestion-trigger-linear",
      "suggestion-trigger-pagerduty",
    ]);
    expect(within(suggestions).getByText("Jira tickets")).toBeInTheDocument();
    expect(within(suggestions).getByText("Slack messages")).toBeInTheDocument();
    expect(within(suggestions).getByText("GitHub")).toBeInTheDocument();
    expect(within(suggestions).getByText("Linear issues")).toBeInTheDocument();
    expect(within(suggestions).getByText("PagerDuty incidents")).toBeInTheDocument();
  });

  it("shows suggestions below populated lists and the full gallery on Templates", () => {
    setTasks([task()]);
    renderPage();
    expect(screen.getByTestId("tasks-list")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Suggested Automations" })).toBeVisible();
    expect(screen.queryByText("Checks every hour")).not.toBeInTheDocument();
    selectView("Templates");
    expect(screen.queryByText("Checks every hour")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tasks-list")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tasks-search")).not.toBeInTheDocument();
    const suggestions = screen.getByTestId("tasks-suggestions");
    expect(screen.getByRole("tab", { name: "Templates" })).toHaveAttribute("aria-selected", "true");
    const cards = within(suggestions).getAllByTestId(/^suggestion-/);
    expect(cards).toHaveLength(5);
    expect(cards.map((c) => c.getAttribute("data-testid"))).toEqual([
      "suggestion-trigger-jira",
      "suggestion-trigger-slack",
      "suggestion-trigger-github",
      "suggestion-trigger-linear",
      "suggestion-trigger-pagerduty",
    ]);
    const jiraCard = within(suggestions).getByTestId("suggestion-trigger-jira");
    expect(jiraCard).toHaveTextContent("Jira tickets");
    expect(jiraCard).toHaveTextContent("Watch for matching tickets");
    expect(jiraCard).toHaveTextContent("Requires a Jira MCP server.");
    expect(screen.queryByRole("button", { name: /Show all/ })).not.toBeInTheDocument();
    expect(jiraCard).toHaveAccessibleDescription(
      "Watch for matching tickets and start a session to investigate or work on each one. Requires a Jira MCP server.",
    );
  });

  it("hides suggestions while filtering or searching and restores them when cleared", () => {
    setTasks([task({ state: "active" })]);
    renderPage();
    expect(screen.getByTestId("tasks-filter-all")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-suggestions")).toBeVisible();

    fireEvent.click(screen.getByTestId("tasks-filter-active"));
    expect(screen.getByTestId("tasks-list")).toBeInTheDocument();
    expect(screen.queryByTestId("tasks-suggestions")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("tasks-filter-paused"));
    expect(screen.getByText("No automations found")).toBeInTheDocument();
    expect(screen.queryByText("No tasks match your filters")).toBeNull();
    expect(screen.queryByText("Try a different search or filter.")).toBeNull();
    expect(screen.getByTestId("tasks-filter-all")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-filter-active")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-filter-paused")).toBeInTheDocument();
    expect(screen.queryByTestId("tasks-suggestions")).toBeNull();

    fireEvent.click(screen.getByTestId("tasks-filter-all"));
    expect(screen.getByTestId("tasks-suggestions")).toBeVisible();
    fireEvent.change(screen.getByTestId("tasks-search"), { target: { value: "Nightly" } });
    expect(screen.getByTestId("tasks-list")).toBeInTheDocument();
    expect(screen.queryByTestId("tasks-suggestions")).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("tasks-search"), { target: { value: "missing" } });
    expect(screen.getByText("No automations found")).toBeVisible();
    expect(screen.queryByTestId("tasks-suggestions")).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("tasks-search"), { target: { value: "" } });
    expect(screen.getByTestId("tasks-suggestions")).toBeVisible();
  });
});

describe("sort order", () => {
  it("orders ACTIVE by soonest next-run first, PAUSED last", () => {
    // Pin now to midnight UTC so daily BYHOUR next-runs are deterministic:
    // 06:00 fires before 18:00 today; the paused task must sink to the bottom
    // regardless of its (earlier) schedule.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    try {
      setTasks([
        // Given in a deliberately "wrong" order to prove the sort runs.
        task({ id: "p", name: "Paused early", state: "paused", rrule: "FREQ=DAILY;BYHOUR=1" }),
        task({ id: "late", name: "Active late", state: "active", rrule: "FREQ=DAILY;BYHOUR=18" }),
        task({ id: "soon", name: "Active soon", state: "active", rrule: "FREQ=DAILY;BYHOUR=6" }),
      ]);
      renderPage();
      const names = screen
        .getAllByTestId("scheduled-task-row")
        .map((r) => r.querySelector(".font-semibold")?.textContent);
      expect(names).toEqual(["Active soon", "Active late", "Paused early"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    {
      // A yearly COUNT=1 rule has no next run the client can compute, so only the
      // server's next_run_at puts a reminder due in two minutes first.
      label: "a one-time reminder by the server's next run",
      tasks: [
        task({ id: "daily", name: "Daily", state: "active", rrule: "FREQ=DAILY;BYHOUR=6" }),
        task({
          id: "reminder",
          name: "Reminder: check CI",
          state: "active",
          rrule: "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=2;BYSECOND=0;COUNT=1",
          timezone: "UTC",
          nextRunAt: "2026-01-01T00:02:00+00:00",
        }),
      ],
      expected: ["Reminder: check CI", "Daily"],
    },
    {
      label: "recurring tasks by the server's next run, falling back on a bad one",
      tasks: [
        // The browser estimates 06:00, but the server says 23:00: the server wins.
        task({
          id: "server-late",
          name: "Server says late",
          state: "active",
          rrule: "FREQ=DAILY;BYHOUR=6",
          nextRunAt: "2026-01-01T23:00:00+00:00",
        }),
        // An unparseable server time falls back to the browser's 12:00 estimate.
        task({
          id: "fallback",
          name: "Bad server time",
          state: "active",
          rrule: "FREQ=DAILY;BYHOUR=12",
          timezone: "UTC",
          nextRunAt: "not a time",
        }),
      ],
      expected: ["Bad server time", "Server says late"],
    },
  ])("sorts $label", ({ tasks, expected }) => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    try {
      setTasks(tasks);
      renderPage();
      const names = screen
        .getAllByTestId("scheduled-task-row")
        .map((r) => r.querySelector(".font-semibold")?.textContent);
      expect(names).toEqual(expected);
    } finally {
      vi.useRealTimers();
    }
  });

  it("sorts every row by name when Name is picked, and back by next run", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    try {
      setTasks([
        task({ id: "z", name: "Zulu", state: "active", rrule: "FREQ=DAILY;BYHOUR=6" }),
        task({ id: "m", name: "Mike", state: "active", rrule: "FREQ=DAILY;BYHOUR=18" }),
        task({ id: "a", name: "Alpha", state: "paused", rrule: "FREQ=DAILY;BYHOUR=1" }),
      ]);
      renderPage();
      const names = () =>
        screen
          .getAllByTestId("scheduled-task-row")
          .map((r) => r.querySelector(".font-semibold")?.textContent);
      expect(screen.getByTestId("tasks-sort")).toHaveTextContent("Next run");
      expect(names()).toEqual(["Zulu", "Mike", "Alpha"]);

      fireEvent.click(screen.getByTestId("tasks-sort"));
      fireEvent.click(screen.getByRole("option", { name: "Name" }));
      expect(names()).toEqual(["Alpha", "Mike", "Zulu"]);

      fireEvent.click(screen.getByTestId("tasks-sort"));
      fireEvent.click(screen.getByRole("option", { name: "Next run" }));
      expect(names()).toEqual(["Zulu", "Mike", "Alpha"]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("filtering + search", () => {
  it("filters to Active / Paused via the tabs", () => {
    setTasks([
      task({ id: "a", name: "Active task", state: "active" }),
      task({ id: "p", name: "Paused task", state: "paused" }),
    ]);
    renderPage();
    expect(screen.getAllByTestId("scheduled-task-row")).toHaveLength(2);
    expect(screen.getByLabelText("Filter automations")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-filter-all")).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByTestId("tasks-filter-paused"));
    expect(screen.getByTestId("tasks-filter-all")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("tasks-filter-paused")).toHaveAttribute("aria-pressed", "true");
    let rows = screen.getAllByTestId("scheduled-task-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Paused task")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("tasks-filter-active"));
    rows = screen.getAllByTestId("scheduled-task-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Active task")).toBeInTheDocument();
  });

  it("filters by name via the search box", () => {
    setTasks([task({ id: "a", name: "Nightly triage" }), task({ id: "b", name: "PR sweep" })]);
    renderPage();
    fireEvent.change(screen.getByTestId("tasks-search"), { target: { value: "sweep" } });
    const rows = screen.getAllByTestId("scheduled-task-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("PR sweep")).toBeInTheDocument();
  });
});

describe("New automation entry", () => {
  it("opens the setup form directly", () => {
    setTasks([]);
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute("data-initial-name", "");
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute("data-initial-prompt", "");
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute("data-initial-trigger", "");
  });

  it.each(["", "/mount"])("redirects old template links to the gallery under '%s'", (basename) => {
    setTasks([task()]);
    renderPage("/tasks/templates", basename);
    expect(screen.getByRole("heading", { name: "Automations" })).toBeInTheDocument();
    expect(screen.queryByTestId("tasks-list")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Automation templates" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Templates" })).toHaveAttribute("aria-selected", "true");
    selectView("Your automations");
    expect(screen.getByTestId("tasks-list")).toBeInTheDocument();
  });

  it("opens setup even when the task list failed to load", () => {
    setTasks([], { isError: true });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    expect(screen.getByTestId("manual-dialog-open")).toBeInTheDocument();
  });
});

describe("automation view navigation", () => {
  it("reports one analytics event per tab change, including pointer focus activation", () => {
    setTasks([]);
    renderPage();
    expect(analytics).toHaveBeenCalledWith({ type: "page_view", pageId: "tasks" });
    selectView("Templates");
    expect(analytics).toHaveBeenCalledWith({ type: "page_view", pageId: "tasks.templates" });
    expect(analytics).toHaveBeenCalledWith({
      type: "value_change",
      componentId: "tasks.view",
      componentKind: "tabs",
      value: "templates",
    });
    selectView("Templates");
    expect(analytics.mock.calls.filter(([event]) => event.type === "value_change")).toHaveLength(1);
    expect(analytics.mock.calls.filter(([event]) => event.type === "page_view")).toEqual([
      [{ type: "page_view", pageId: "tasks" }],
      [{ type: "page_view", pageId: "tasks.templates" }],
    ]);
    selectView("Your automations");
    expect(analytics.mock.calls.filter(([event]) => event.type === "page_view")).toEqual([
      [{ type: "page_view", pageId: "tasks" }],
      [{ type: "page_view", pageId: "tasks.templates" }],
      [{ type: "page_view", pageId: "tasks" }],
    ]);
    expect(analytics.mock.calls.filter(([event]) => event.type === "value_change")).toHaveLength(2);
    expect(analytics).toHaveBeenCalledWith({
      type: "value_change",
      componentId: "tasks.view",
      componentKind: "tabs",
      value: "yours",
    });
  });

  it("keeps search and filter selections when switching views", () => {
    setTasks([
      task({ name: "Daily review", state: "paused" }),
      task({ id: "other", name: "Other task" }),
    ]);
    renderPage();
    fireEvent.change(screen.getByTestId("tasks-search"), { target: { value: "Daily" } });
    fireEvent.click(screen.getByTestId("tasks-filter-paused"));
    selectView("Templates");
    expect(screen.getByRole("region", { name: "Automation templates" })).toBeInTheDocument();
    selectView("Your automations");
    expect(screen.getByTestId("tasks-search")).toHaveValue("Daily");
    expect(screen.getByTestId("tasks-filter-paused")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getAllByTestId("scheduled-task-row")).toHaveLength(1);
  });

  it("opens both creation paths from a directly linked Templates tab even when tasks fail to load", () => {
    setTasks(undefined, { isError: true });
    renderPage("/tasks?view=templates");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("suggestion-trigger-github"));
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute(
      "data-initial-trigger",
      "github",
    );
    fireEvent.click(screen.getByRole("button", { name: "Close setup" }));
    fireEvent.click(screen.getByTestId("new-task-button"));
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute("data-initial-trigger", "");
  });
});

describe("after creating an automation", () => {
  beforeEach(() => setTasks([task()]));

  it("leaves the template gallery for the list of automations", () => {
    renderPage("/tasks?view=templates");
    expect(screen.getByRole("region", { name: "Automation templates" })).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("suggestion-trigger-jira"));
    expect(showToast).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Create setup" }));

    expect(screen.queryByRole("region", { name: "Automation templates" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Your automations" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(showToast).toHaveBeenCalledExactlyOnceWith("Automation created");
  });

  it("stays on the list when created from the list", () => {
    renderPage("/tasks");
    fireEvent.click(screen.getAllByRole("button", { name: "New" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "Create setup" }));
    expect(screen.getByRole("tab", { name: "Your automations" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(showToast).toHaveBeenCalledExactlyOnceWith("Automation created");
  });

  it("clears a filter and search that would hide the new automation", () => {
    renderPage("/tasks");
    fireEvent.click(screen.getByTestId("tasks-filter-paused"));
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "zzz" } });
    selectView("Templates");
    fireEvent.click(screen.getByTestId("suggestion-trigger-jira"));
    fireEvent.click(screen.getByRole("button", { name: "Create setup" }));

    expect(screen.getByTestId("tasks-filter-all")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByPlaceholderText(/search/i)).toHaveValue("");
  });
});

describe("template prefill", () => {
  it.each(["empty", "active", "paused"] as const)(
    "opens template setup with an %s list",
    (state) => {
      setTasks(state === "empty" ? [] : [task({ state })]);
      renderPage();
      fireEvent.click(screen.getByTestId("suggestion-trigger-jira"));
      const dialog = screen.getByTestId("manual-dialog-open");
      expect(dialog.getAttribute("data-initial-name")).toBe("Jira tickets");
      expect(dialog.getAttribute("data-initial-trigger")).toBe("jira");
      expect(dialog.getAttribute("data-initial-prompt")).toBe("");
      expect(dialog).toHaveAttribute("data-initial-template", "jira");
      expect(analytics).toHaveBeenCalledWith({
        type: "click",
        componentId: "tasks.template.jira",
        componentKind: "button",
      });
    },
  );

  it("reseeds from another card after closing, and starts manual creation empty", () => {
    setTasks([]);
    renderPage();

    // Each card seeds a fresh form.
    fireEvent.click(screen.getByTestId("suggestion-trigger-jira"));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-name")).toBe(
      "Jira tickets",
    );
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute(
      "data-initial-template",
      "jira",
    );

    fireEvent.click(screen.getByRole("button", { name: "Close setup" }));
    expect(screen.queryByTestId("manual-dialog-open")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("suggestion-trigger-slack"));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-name")).toBe(
      "Slack messages",
    );
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute(
      "data-initial-template",
      "slack",
    );

    fireEvent.click(screen.getByRole("button", { name: "Close setup" }));
    // Manual creation must not inherit the previous template.
    fireEvent.click(screen.getByTestId("new-task-button"));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-name")).toBe("");
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute("data-initial-template", "");
  });
});

describe("row actions", () => {
  it.each([
    [new ScheduledTaskApiError("conflict", 409, "CONFLICT"), "This run is already in progress"],
    [new ScheduledTaskApiError("server error", 503, null), "Couldn't start the run"],
    [new TypeError("Failed to fetch"), "Couldn't start the run"],
  ])("shows Run now feedback for %s", (error, message) => {
    setTasks([task()]);
    renderPage();
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Run now" }));

    expect(runNowMutate).toHaveBeenCalledWith("st_1", expect.any(Object));
    expect(showToast).not.toHaveBeenCalled();

    runNowMutate.mock.calls[0][1].onError(error);
    expect(showToast).toHaveBeenCalledExactlyOnceWith(message);
  });

  it("confirms Run now with a run history hint only after the request succeeds", () => {
    setTasks([task()]);
    renderPage();
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Run now" }));

    expect(runNowMutate).toHaveBeenCalledWith("st_1", expect.any(Object));
    expect(showToast).not.toHaveBeenCalled();

    runNowMutate.mock.calls[0][1].onSuccess();
    expect(showToast).toHaveBeenCalledExactlyOnceWith(
      "Run triggered, it will start shortly. Open automation details to check run history.",
    );
  });

  it("opens edit mode for a task from the row menu", () => {
    setTasks([task({ id: "st_edit", name: "Morning brief" })]);
    renderPage();
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    fireEvent.click(screen.getByTestId("task-edit"));
    const dialog = screen.getByTestId("manual-dialog-open");
    expect(dialog.getAttribute("data-editing-task-id")).toBe("st_edit");
    expect(dialog.getAttribute("data-editing-task-name")).toBe("Morning brief");
    expect(dialog.getAttribute("data-initial-name")).toBe("");
  });

  it("pauses an active task via the row menu (Pause label reflects state)", () => {
    setTasks([task({ id: "st_1", state: "active" })]);
    renderPage();
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    // Active → the toggle item reads "Pause".
    expect(screen.getByTestId("task-pause-toggle")).toHaveTextContent("Pause");
    fireEvent.click(screen.getByTestId("task-pause-toggle"));
    expect(mutate).toHaveBeenCalledWith({ id: "st_1", input: { state: "paused" } });
  });

  it("deletes a task via the row menu", () => {
    setTasks([task({ id: "st_1" })]);
    renderPage();
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    fireEvent.click(screen.getByTestId("task-delete"));
    expect(deleteMutate).toHaveBeenCalledWith("st_1");
  });
});

describe("cloning from the row menu", () => {
  const CLONE_INFO = {
    ...TRIGGER_INFO,
    scheduled_task_clone: true,
    features: { ...TRIGGER_INFO.features, automations_v2: true },
  };

  it("offers Clone only on a server that can clone", () => {
    setTasks([task()]);
    renderPage();
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    expect(screen.queryByTestId("task-clone")).toBeNull();
  });

  it("opens the copy and says what's left once the clone is created", () => {
    setTasks([task({ id: "st_1", state: "active" })]);
    renderPage("/tasks", "", CLONE_INFO);
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Clone" }));

    expect(cloneMutate).toHaveBeenCalledWith(
      expect.objectContaining({ id: "st_1" }),
      expect.any(Object),
    );
    expect(showToast).not.toHaveBeenCalled();

    act(() =>
      cloneMutate.mock.calls[0][1].onSuccess(
        task({ id: "st_2", nextRunAt: "2026-11-20T09:00:00+00:00" }),
      ),
    );
    expect(showToast).toHaveBeenCalledExactlyOnceWith(
      "Automation cloned. Pause or delete the original so they don't both run.",
    );
    expect(screen.getByTestId("task-detail-route")).toBeInTheDocument();
  });

  it("stays on the list when the server has no automation pages", () => {
    setTasks([task({ id: "st_1" })]);
    renderPage("/tasks", "", { ...TRIGGER_INFO, scheduled_task_clone: true });
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Clone" }));

    act(() => cloneMutate.mock.calls[0][1].onSuccess(task({ id: "st_2" })));
    expect(showToast).toHaveBeenCalledOnce();
    expect(screen.queryByTestId("task-detail-route")).toBeNull();
    expect(screen.getByTestId("tasks-list")).toBeInTheDocument();
  });

  it("stays on the list and shows the server's reason when the clone is refused", () => {
    setTasks([task()]);
    renderPage("/tasks", "", CLONE_INFO);
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Clone" }));

    cloneMutate.mock.calls[0][1].onError(
      new ScheduledTaskApiError("Only an automation can be cloned", 400, "invalid_input"),
    );
    expect(showToast).toHaveBeenCalledExactlyOnceWith(
      "Couldn't clone the automation: Only an automation can be cloned",
    );
    expect(screen.getByTestId("tasks-list")).toBeInTheDocument();
  });
});

describe("sharing from the row menu", () => {
  const SHARING_INFO = { ...TRIGGER_INFO, scheduled_task_sharing: true };

  function openRowMenu(
    viewerId: string,
    info: typeof SERVER_INFO_OFFLINE_FALLBACK,
    overrides: Partial<ScheduledTask> = {},
  ) {
    vi.mocked(useViewerId).mockReturnValue(viewerId);
    setTasks([task({ id: "st_share", ownerUserId: "alice@example.com", ...overrides })]);
    renderPage("/tasks", "", info);
    // A known viewer gets the first-visit guide; dismiss it.
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
  }

  // An older server sends no `shareable`.
  it.each([
    ["shareable", { shareable: true }],
    ["from an older server", {}],
  ])("opens the share dialog for an automation the viewer owns (%s)", (_label, overrides) => {
    openRowMenu("alice@example.com", SHARING_INFO, overrides);
    fireEvent.click(screen.getByTestId("task-share"));
    expect(screen.getByTestId("share-dialog-open")).toHaveAttribute(
      "data-automation-id",
      "st_share",
    );
  });

  const SHARING_OFF = "Sharing has been disabled for this Omnigent server.";
  const LEGACY = "This is a legacy automation. Recreate it to share it.";
  it.each([
    ["while the server turns sharing off", "off" as const, true, SHARING_OFF],
    ["on a legacy automation", undefined, false, LEGACY],
    // Sharing off wins.
    ["on a legacy automation while sharing is off", "off" as const, false, SHARING_OFF],
  ])("disables Share with its reason %s", async (_label, sharingMode, shareable, reason) => {
    openRowMenu(
      "alice@example.com",
      { ...SHARING_INFO, ...(sharingMode && { sharing_mode: sharingMode }) },
      { shareable },
    );
    const share = screen.getByTestId("task-share");
    expect(share).toHaveAttribute("aria-disabled", "true");
    fireEvent.focus(share);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(reason);
    fireEvent.click(share);
    expect(screen.queryByTestId("share-dialog-open")).toBeNull();
  });

  it.each([
    ["another user", "bob@example.com", SHARING_INFO],
    ["a server that cannot share automations", "alice@example.com", TRIGGER_INFO],
  ])("offers no Share to %s", (_label, viewerId, info) => {
    openRowMenu(viewerId, info);
    expect(screen.getByTestId("task-edit")).toBeInTheDocument();
    expect(screen.queryByTestId("task-share")).toBeNull();
  });
});

describe("automations shared with the viewer", () => {
  const SHARING_INFO = {
    ...TRIGGER_INFO,
    scheduled_task_sharing: true,
    scheduled_task_shared_with_me: true,
  };
  const shared = (overrides: Partial<ScheduledTask> = {}) =>
    task({
      id: "st_shared",
      name: "Team digest",
      ownerUserId: "alice@example.com",
      viewerRole: "viewer",
      ownerDisplayName: "Alice",
      ...overrides,
    });

  function openSharedTab(info: typeof SERVER_INFO_OFFLINE_FALLBACK = SHARING_INFO) {
    vi.mocked(useViewerId).mockReturnValue("bob@example.com");
    setTasks([task({ id: "st_mine", name: "Mine", ownerUserId: "bob@example.com" })]);
    renderPage("/tasks?view=shared", "", info);
    // Without the tab the page falls back to the viewer's list, whose guide shows once.
    const dismissGuide = screen.queryByRole("button", { name: "Maybe later" });
    if (dismissGuide) fireEvent.click(dismissGuide);
  }

  it("lists them on their own tab, naming each owner", () => {
    setSharedPages([{ tasks: [shared()], nextCursor: null }]);
    openSharedTab();
    expect(hooks.useSharedScheduledTasks).toHaveBeenLastCalledWith(true);
    expect(screen.getByRole("tab", { name: "Shared with me" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const list = screen.getByTestId("shared-tasks-list");
    const row = within(list).getByTestId("scheduled-task-row");
    expect(within(row).getByText("Team digest")).toBeInTheDocument();
    expect(within(row).getByTestId("task-owner")).toHaveTextContent("Shared by Alice");
    expect(within(list).queryByText("Mine")).toBeNull();
  });

  it("shows every action disabled with why, and takes none", async () => {
    setSharedPages([{ tasks: [shared()], nextCursor: null }]);
    openSharedTab({ ...SHARING_INFO, scheduled_task_clone: true });
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    for (const testId of [
      "task-run-now",
      "task-edit",
      "task-share",
      "task-clone",
      "task-pause-toggle",
      "task-delete",
    ]) {
      const item = screen.getByTestId(testId);
      expect(item).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(item);
    }
    expect(runNowMutate).not.toHaveBeenCalled();
    expect(mutate).not.toHaveBeenCalled();
    expect(deleteMutate).not.toHaveBeenCalled();
    expect(cloneMutate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("manual-dialog-open")).toBeNull();
    expect(screen.queryByTestId("share-dialog-open")).toBeNull();
    fireEvent.focus(screen.getByTestId("task-clone").parentElement!);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "Only the automation's owner can clone it.",
    );
  });

  it("loads more on request, and past a page that came back empty", () => {
    setSharedPages([{ tasks: [], nextCursor: "st_0" }]);
    openSharedTab();
    // Nothing to show yet, so the next page loads by itself.
    expect(fetchNextSharedPage).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("shared-tasks-empty-state")).toBeNull();

    fetchNextSharedPage.mockReset();
    cleanup();
    setSharedPages([{ tasks: [shared()], nextCursor: "st_shared" }]);
    openSharedTab();
    expect(fetchNextSharedPage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("shared-tasks-show-more"));
    expect(fetchNextSharedPage).toHaveBeenCalledTimes(1);
  });

  it("stops loading past an empty page once a page fails, until Retry", () => {
    // An empty page with more to come, whose next page failed.
    setSharedPages([{ tasks: [], nextCursor: "st_0" }], { isError: true });
    openSharedTab();

    expect(fetchNextSharedPage).not.toHaveBeenCalled();
    expect(screen.getByTestId("shared-tasks-load-error")).toBeInTheDocument();
  });

  it("says when nothing is shared with the viewer", () => {
    setSharedPages([{ tasks: [], nextCursor: null }]);
    openSharedTab();
    expect(screen.getByTestId("shared-tasks-empty-state")).toHaveTextContent(
      "No automations are shared with you yet",
    );
  });

  it.each([
    ["a server that cannot share automations", TRIGGER_INFO],
    ["a single-user server", { ...SHARING_INFO, single_user: true }],
    [
      "an older server that shares automations but cannot list them",
      { ...TRIGGER_INFO, scheduled_task_sharing: true },
    ],
  ])("has no Shared with me tab on %s", (_label, info) => {
    setSharedPages([{ tasks: [shared()], nextCursor: null }]);
    openSharedTab(info);
    expect(screen.queryByRole("tab", { name: "Shared with me" })).toBeNull();
    expect(hooks.useSharedScheduledTasks).toHaveBeenLastCalledWith(false);
    expect(screen.getByRole("tab", { name: "Your automations" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});

describe("trigger templates", () => {
  it("opens the dialog on the picked template with an absolute session link", () => {
    setTasks([]);
    renderPage();
    fireEvent.click(screen.getByTestId("suggestion-trigger-slack"));
    const dialog = screen.getByTestId("manual-dialog-open");
    expect(dialog.getAttribute("data-initial-trigger")).toBe("slack");
    expect(dialog.getAttribute("data-initial-prompt")).toBe("");
    expect(dialog.getAttribute("data-session-link")).toBe(
      `${window.location.origin}/c/CONVERSATION_ID`,
    );

    fireEvent.click(screen.getByRole("button", { name: "Close setup" }));
    // A later plain open drops the template.
    fireEvent.click(screen.getByTestId("new-task-button"));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-trigger")).toBe("");
  });

  it.each(["jira", "slack", "github", "linear", "pagerduty"])(
    "opens the %s builder from its gallery card",
    (source) => {
      vi.mocked(useViewerId).mockReturnValue("alice");
      setTasks([]);
      renderPage("/tasks?view=templates");
      expect(
        screen.queryByRole("dialog", { name: "Introducing Automations" }),
      ).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId(`suggestion-trigger-${source}`));
      expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute(
        "data-initial-trigger",
        source,
      );
      expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute(
        "data-initial-template",
        source,
      );
      expect(analytics).toHaveBeenCalledWith({
        type: "click",
        componentId: `tasks.template.${source}`,
        componentKind: "button",
      });
    },
  );

  it.each([
    ["can't keep one session per automation", { scheduled_task_session_modes: false }],
    ["has session orchestration turned off", { features: { session_orchestration: false } }],
    // Older servers don't report the feature at all.
    ["doesn't report session orchestration", { features: {} }],
  ])("hides the templates when the server %s", (_name, overrides) => {
    setTasks([]);
    renderPage("/tasks?view=templates", "", { ...TRIGGER_INFO, ...overrides });
    expect(screen.getByText(/Automation templates are not available on this server/)).toBeVisible();
    expect(screen.queryByTestId("tasks-suggestions")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "New" }));
    const dialog = screen.getByTestId("manual-dialog-open");
    expect(dialog).toHaveAttribute("data-initial-name", "");
    expect(dialog).toHaveAttribute("data-initial-prompt", "");
    expect(dialog).toHaveAttribute("data-initial-trigger", "");
    fireEvent.click(screen.getByRole("button", { name: "Close setup" }));
    selectView("Your automations");
    expect(screen.getByText("No automations yet")).toBeVisible();
    expect(screen.queryByTestId("tasks-suggestions")).not.toBeInTheDocument();
  });
});

describe("a link that opens New automation", () => {
  it("opens it filled in once, and New afterwards starts empty", async () => {
    setTasks([]);
    renderPage(
      `/tasks?new=1&name=${encodeURIComponent("Weekly digest")}&prompt=${encodeURIComponent(
        "Summarize the board",
      )}&schedule=${encodeURIComponent("FREQ=WEEKLY;BYDAY=MO")}`,
    );

    const dialog = await screen.findByTestId("manual-dialog-open");
    expect(dialog).toHaveAttribute("data-initial-name", "Weekly digest");
    expect(dialog).toHaveAttribute("data-initial-prompt", "Summarize the board");
    expect(dialog).toHaveAttribute("data-initial-schedule", "FREQ=WEEKLY;BYDAY=MO");

    fireEvent.click(screen.getByRole("button", { name: "Close setup" }));
    expect(screen.queryByTestId("manual-dialog-open")).toBeNull();
    fireEvent.click(screen.getByTestId("new-task-button"));
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute("data-initial-name", "");
  });
});

describe("the list's actions in ⌘K", () => {
  const withQuickPick = (on: boolean) => ({
    ...TRIGGER_INFO,
    features: { ...TRIGGER_INFO.features, quick_pick_palette: on },
  });
  /** Markup without React's generated ids, which count every `useId` call in the tree. */
  const markup = (element: HTMLElement) => element.innerHTML.replace(/:r[0-9a-z]+:/g, ":id:");
  const command = (label: string) =>
    registeredPaletteCommands().find((candidate) => candidate.label === label);

  beforeEach(() => {
    listPaletteCommands.renders = 0;
  });
  afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));

  it("mounts nothing with quick_pick_palette off, and the page renders the same", () => {
    setTasks([task()]);
    const { container, unmount } = renderPage("/tasks", "", withQuickPick(false));
    const withoutCommands = markup(container);
    expect(listPaletteCommands.renders).toBe(0);
    expect(registeredPaletteCommands()).toEqual([]);
    unmount();

    // The commands render nothing themselves, so turning the flag on changes no markup.
    const { container: withFlag } = renderPage("/tasks", "", withQuickPick(true));
    expect(listPaletteCommands.renders).toBeGreaterThan(0);
    expect(markup(withFlag)).toBe(withoutCommands);
  });

  it("runs the row menu's Pause and the New button's editor from ⌘K", () => {
    setTasks([task()]);
    renderPage("/tasks", "", withQuickPick(true));
    act(() => command("Pause “Nightly triage”")!.run());
    expect(mutate).toHaveBeenCalledWith({ id: "st_1", input: { state: "paused" } });
    expect(command("Delete “Nightly triage”")).toBeUndefined();
    act(() => command("New automation…")!.run());
    expect(screen.getByTestId("manual-dialog-open")).toBeInTheDocument();
  });
});
