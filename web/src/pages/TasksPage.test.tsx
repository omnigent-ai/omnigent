// Tests for the Scheduled tasks page (`/tasks`): list rendering, the
// Active/Paused filter + search, the Paused badge/dimming, the New automation
// creation menu and templates page, and the pause/delete row actions dispatching the
// mutation hooks.
//
// The scheduled-tasks hooks are mocked at their seam; the create dialog is
// stubbed to a marker so we assert the page opens it without exercising its
// internals (covered by its own tests).

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TasksPage } from "./TasksPage";
import { useViewerId } from "@/hooks/useViewerId";
import * as hooks from "@/hooks/useScheduledTasks";
import { basenamedRouting, RoutingProvider } from "@/lib/routing";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
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
// passed, so we can assert chip-click seeds it and manual open does not.
vi.mock("@/components/scheduled/CreateScheduledTaskDialog", () => ({
  CreateScheduledTaskDialog: ({
    open,
    initialName,
    initialPrompt,
    initialTrigger,
    sessionLink,
    editingTask,
  }: {
    open: boolean;
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
      />
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

afterEach(() => cleanup());

function renderPage(path = "/tasks", basename = "") {
  return render(
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
    </MemoryRouter>,
  );
}

describe("TasksPage list", () => {
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

  it("shows compact suggestion chips in the true empty state", () => {
    setTasks([]);
    renderPage();
    const suggestions = screen.getByTestId("tasks-suggestions");
    expect(within(suggestions).queryByText("Suggestions")).toBeNull();
    const chips = within(suggestions).getAllByTestId(/^suggestion-/);
    expect(chips).toHaveLength(3);
    expect(chips.map((r) => r.getAttribute("data-testid"))).toEqual([
      "suggestion-follow-up-monitor",
      "suggestion-pr-sweep",
      "suggestion-news-digest",
    ]);
    expect(within(suggestions).getByText("Follow-up monitor")).toBeInTheDocument();
    expect(within(suggestions).getByText("PR sweep")).toBeInTheDocument();
    expect(within(suggestions).getByText("News digest")).toBeInTheDocument();
  });

  it("shows compact suggestion chips below populated lists", () => {
    setTasks([task()]);
    renderPage();
    const suggestions = screen.getByTestId("tasks-suggestions");
    expect(within(suggestions).getByText("Suggestions")).toBeInTheDocument();
    const chips = within(suggestions).getAllByTestId(/^suggestion-/);
    expect(chips).toHaveLength(3);
    expect(chips.map((c) => c.getAttribute("data-testid"))).toEqual([
      "suggestion-follow-up-monitor",
      "suggestion-pr-sweep",
      "suggestion-news-digest",
    ]);
    const followUp = within(suggestions).getByTestId("suggestion-follow-up-monitor");
    expect(followUp).toHaveTextContent("Follow-up monitor");
  });

  it("hides Suggestions in filtered-empty states after tasks exist", () => {
    setTasks([task({ state: "active" })]);
    renderPage();
    expect(screen.getByTestId("tasks-filter-all")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-suggestions")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("tasks-filter-paused"));
    expect(screen.getByText("No automations found")).toBeInTheDocument();
    expect(screen.queryByText("No tasks match your filters")).toBeNull();
    expect(screen.queryByText("Try a different search or filter.")).toBeNull();
    expect(screen.getByTestId("tasks-filter-all")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-filter-active")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-filter-paused")).toBeInTheDocument();
    expect(screen.queryByTestId("tasks-suggestions")).toBeNull();

    fireEvent.click(screen.getByTestId("tasks-filter-all"));
    expect(screen.getByTestId("tasks-suggestions")).toBeInTheDocument();
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

describe("New automation menu", () => {
  it("opens an empty manual form only after selecting Create manually", () => {
    setTasks([]);
    renderPage();
    const button = screen.getByRole("button", { name: "New automation" });
    fireEvent.pointerDown(button, { button: 0 });
    expect(screen.queryByTestId("manual-dialog-open")).toBeNull();
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Create manually",
      "Create from template",
    ]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Create manually" }));
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute("data-initial-name", "");
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute("data-initial-trigger", "");
  });

  it.each(["", "/mount"])("opens templates and returns to the list under '%s'", (basename) => {
    setTasks([task()]);
    renderPage("/tasks", basename);
    fireEvent.pointerDown(screen.getByTestId("new-task-button"), { button: 0 });
    const templates = screen.getByRole("menuitem", { name: "Create from template" });
    expect(templates).toHaveAttribute("href", `${basename}/tasks/templates`);
    fireEvent.click(templates);
    expect(screen.getByRole("heading", { name: "Automation templates" })).toBeInTheDocument();
    expect(screen.queryByTestId("tasks-list")).toBeNull();
    expect(screen.queryByTestId("manual-dialog-open")).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "Back to automations" }));
    expect(screen.getByTestId("tasks-list")).toBeInTheDocument();
  });

  it("opens a template from a direct link even when the task list failed to load", () => {
    setTasks([], { isError: true });
    renderPage("/tasks/templates");
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "PR sweep" }));
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute(
      "data-initial-name",
      "PR sweep",
    );
  });
});

describe("suggestion prefill", () => {
  it("seeds the dialog with the picked suggestion's name + prompt", () => {
    setTasks([]);
    renderPage();
    fireEvent.click(screen.getByTestId("suggestion-follow-up-monitor"));
    const dialog = screen.getByTestId("manual-dialog-open");
    // The fuller prefill.name/prompt are passed (NOT the short chip label).
    expect(dialog.getAttribute("data-initial-name")).toBe("Follow-up monitor");
    expect(dialog.getAttribute("data-initial-prompt")).toContain("Review recent email");
  });

  it("opens EMPTY from the 'Create manually' menu item (no prefill)", () => {
    setTasks([]);
    renderPage();
    fireEvent.pointerDown(screen.getByTestId("new-task-button"), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Create manually" }));
    const dialog = screen.getByTestId("manual-dialog-open");
    expect(dialog.getAttribute("data-initial-name")).toBe("");
    expect(dialog.getAttribute("data-initial-prompt")).toBe("");
  });

  it("reseeds when switching chips, and does not leak a stale prefill into a manual open", () => {
    setTasks([]);
    renderPage();

    // Open from one chip → seeded.
    fireEvent.click(screen.getByTestId("suggestion-follow-up-monitor"));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-name")).toBe(
      "Follow-up monitor",
    );

    // The stub dialog reports open via the `open` prop; simulate a close by
    // clicking a different chip (reseed) — the new suggestion's values win.
    fireEvent.click(screen.getByTestId("suggestion-pr-sweep"));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-name")).toBe(
      "PR sweep",
    );

    // Now the plain manual open must be EMPTY — no stale prefill from the chips.
    fireEvent.pointerDown(screen.getByTestId("new-task-button"), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Create manually" }));
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
  const TRIGGER_INFO = {
    ...SERVER_INFO_OFFLINE_FALLBACK,
    scheduled_task_session_modes: true,
    features: { session_orchestration: true },
  };

  function renderWithSessionModes(
    info: typeof SERVER_INFO_OFFLINE_FALLBACK = TRIGGER_INFO,
    templatesOnly = false,
  ) {
    return render(
      <CapabilitiesProvider info={info}>
        <MemoryRouter initialEntries={["/tasks"]}>
          <TasksPage templatesOnly={templatesOnly} />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );
  }

  it.each([false, true])("shows all existing templates (templatesOnly=%s)", (templatesOnly) => {
    setTasks([task()]);
    renderWithSessionModes(TRIGGER_INFO, templatesOnly);
    const chips = within(screen.getByTestId("tasks-suggestions")).getAllByTestId(/^suggestion-/);
    expect(chips.map((c) => c.getAttribute("data-testid"))).toEqual([
      "suggestion-trigger-jira",
      "suggestion-trigger-slack",
      "suggestion-trigger-github",
      "suggestion-follow-up-monitor",
      "suggestion-pr-sweep",
      "suggestion-news-digest",
    ]);
  });

  it("opens the dialog on the picked template with an absolute session link", () => {
    setTasks([]);
    renderWithSessionModes();
    fireEvent.click(screen.getByTestId("suggestion-trigger-slack"));
    const dialog = screen.getByTestId("manual-dialog-open");
    expect(dialog.getAttribute("data-initial-trigger")).toBe("slack");
    expect(dialog.getAttribute("data-initial-prompt")).toBe("");
    expect(dialog.getAttribute("data-session-link")).toBe(
      `${window.location.origin}/c/CONVERSATION_ID`,
    );

    // A later plain open drops the template.
    fireEvent.pointerDown(screen.getByTestId("new-task-button"), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Create manually" }));
    expect(screen.getByTestId("manual-dialog-open").getAttribute("data-initial-trigger")).toBe("");
  });

  it.each(["jira", "slack", "github"])("opens the %s builder from the templates page", (source) => {
    vi.mocked(useViewerId).mockReturnValue("alice");
    setTasks([]);
    renderWithSessionModes(TRIGGER_INFO, true);
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
  ])("hides the chips when the server %s", (_name, overrides) => {
    setTasks([]);
    renderWithSessionModes({ ...TRIGGER_INFO, ...overrides }, true);
    expect(screen.getByTestId("suggestion-follow-up-monitor")).toBeInTheDocument();
    expect(screen.queryByTestId("suggestion-trigger-jira")).toBeNull();
  });
});
