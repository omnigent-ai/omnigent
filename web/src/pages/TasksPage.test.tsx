// Tests for /tasks: lists, filters, gallery tabs, and row actions.
// Scheduled-task hooks are mocked; the creation dialog exposes its seed props and close action.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TasksPage } from "./TasksPage";
import { useViewerId } from "@/hooks/useViewerId";
import * as hooks from "@/hooks/useScheduledTasks";
import { basenamedRouting, RoutingProvider } from "@/lib/routing";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { setOmnigentHostConfig } from "@/lib/host";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: vi.fn() }));

vi.mock("@/hooks/useScheduledTasks", () => ({
  useScheduledTasks: vi.fn(),
  useUpdateScheduledTask: vi.fn(),
  useDeleteScheduledTask: vi.fn(),
  useRunScheduledTaskNow: vi.fn(),
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
    sessionLink,
    editingTask,
  }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    initialName?: string;
    initialPrompt?: string;
    initialTrigger?: string | null;
    sessionLink?: string;
    editingTask?: ScheduledTask | null;
  }) =>
    open ? (
      <div
        data-testid="manual-dialog-open"
        data-initial-name={initialName ?? ""}
        data-initial-prompt={initialPrompt ?? ""}
        data-initial-trigger={initialTrigger ?? ""}
        data-session-link={sessionLink ?? ""}
        data-editing-task-id={editingTask?.id ?? ""}
        data-editing-task-name={editingTask?.name ?? ""}
      >
        <button type="button" onClick={() => onOpenChange(false)}>
          Close setup
        </button>
      </div>
    ) : null,
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

beforeEach(() => {
  localStorage.clear();
  vi.mocked(useViewerId).mockReturnValue(null);
  mutate.mockReset();
  deleteMutate.mockReset();
  runNowMutate.mockReset();
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
      <MemoryRouter initialEntries={[`${basename}${path}`]}>
        <RoutingProvider value={basenamedRouting(basename)}>
          <Routes>
            <Route path={`${basename}/tasks`} element={<TasksPage />} />
            <Route
              path={`${basename}/tasks/templates`}
              element={<TasksPage key="templates" templatesOnly />}
            />
          </Routes>
        </RoutingProvider>
      </MemoryRouter>
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

  it("renders the title, subtitle and task rows with schedule text", () => {
    setTasks([task()]);
    renderPage();
    expect(screen.getByText("Automations")).toBeInTheDocument();
    expect(screen.getByText(/Run agent sessions on a recurring schedule/i)).toBeInTheDocument();
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
    expect(within(suggestions).getByText("Start from a template")).toBeInTheDocument();
    const cards = within(suggestions).getAllByTestId(/^suggestion-/);
    expect(cards).toHaveLength(3);
    expect(cards.map((r) => r.getAttribute("data-testid"))).toEqual([
      "suggestion-trigger-jira",
      "suggestion-trigger-slack",
      "suggestion-trigger-github",
    ]);
    expect(within(suggestions).getByText("Jira tickets")).toBeInTheDocument();
    expect(within(suggestions).getByText("Slack messages")).toBeInTheDocument();
    expect(within(suggestions).getByText("GitHub issues")).toBeInTheDocument();
  });

  it("shows the gallery on Templates instead of below populated lists", () => {
    setTasks([task()]);
    renderPage();
    expect(screen.queryByTestId("tasks-suggestions")).not.toBeInTheDocument();
    selectView("Templates");
    expect(screen.queryByTestId("tasks-list")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tasks-search")).not.toBeInTheDocument();
    const suggestions = screen.getByTestId("tasks-suggestions");
    expect(screen.getByRole("tab", { name: "Templates" })).toHaveAttribute("aria-selected", "true");
    const cards = within(suggestions).getAllByTestId(/^suggestion-/);
    expect(cards).toHaveLength(3);
    expect(cards.map((c) => c.getAttribute("data-testid"))).toEqual([
      "suggestion-trigger-jira",
      "suggestion-trigger-slack",
      "suggestion-trigger-github",
    ]);
    const jiraCard = within(suggestions).getByTestId("suggestion-trigger-jira");
    expect(jiraCard).toHaveTextContent("Jira tickets");
    expect(jiraCard).toHaveTextContent("Watch for matching tickets");
    expect(jiraCard).toHaveTextContent("Requires a Jira MCP server.");
    expect(jiraCard).toHaveTextContent("Checks every hour");
    expect(screen.queryByRole("button", { name: /Show all/ })).not.toBeInTheDocument();
    expect(jiraCard).toHaveAccessibleDescription(
      "Watch for matching tickets and start a session to investigate or work on each one. Checks every hour Requires a Jira MCP server.",
    );
  });

  it("keeps templates out of populated and filtered-empty lists", () => {
    setTasks([task({ state: "active" })]);
    renderPage();
    expect(screen.getByTestId("tasks-filter-all")).toBeInTheDocument();
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
    expect(screen.queryByTestId("tasks-suggestions")).not.toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("button", { name: "New automation" }));
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
    fireEvent.click(screen.getByRole("button", { name: "New automation" }));
    expect(screen.getByTestId("manual-dialog-open")).toBeInTheDocument();
  });
});

describe("automation view navigation", () => {
  it("reports one analytics event per tab change, including pointer focus activation", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    setTasks([]);
    renderPage();
    selectView("Templates");
    expect(analytics).toHaveBeenCalledExactlyOnceWith({
      type: "value_change",
      componentId: "tasks.view",
      componentKind: "tabs",
      value: "templates",
    });
    selectView("Templates");
    expect(analytics).toHaveBeenCalledTimes(1);
    selectView("Your automations");
    expect(analytics).toHaveBeenCalledTimes(2);
    expect(analytics).toHaveBeenLastCalledWith({
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

describe("template prefill", () => {
  it("opens the integration builder with the selected template name", () => {
    setTasks([]);
    renderPage();
    fireEvent.click(screen.getByTestId("suggestion-trigger-jira"));
    const dialog = screen.getByTestId("manual-dialog-open");
    expect(dialog.getAttribute("data-initial-name")).toBe("Jira tickets");
    expect(dialog.getAttribute("data-initial-trigger")).toBe("jira");
    expect(dialog.getAttribute("data-initial-prompt")).toBe("");
  });

  it("reseeds from another card after closing, and starts manual creation empty", () => {
    setTasks([]);
    renderPage();

    // Each card seeds a fresh form.
    fireEvent.click(screen.getByTestId("suggestion-trigger-jira"));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-name")).toBe(
      "Jira tickets",
    );

    fireEvent.click(screen.getByRole("button", { name: "Close setup" }));
    expect(screen.queryByTestId("manual-dialog-open")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("suggestion-trigger-slack"));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-name")).toBe(
      "Slack messages",
    );

    fireEvent.click(screen.getByRole("button", { name: "Close setup" }));
    // Manual creation must not inherit the previous template.
    fireEvent.click(screen.getByTestId("new-task-button"));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-name")).toBe("");
  });
});

describe("row actions", () => {
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

  it.each(["jira", "slack", "github"])("opens the %s builder from its gallery card", (source) => {
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
  });

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
    fireEvent.click(screen.getByRole("button", { name: "New automation" }));
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
