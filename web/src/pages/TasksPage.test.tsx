// Tests for the Scheduled tasks page (`/tasks`): list rendering, the
// Active/Paused filter + search, the Paused badge/dimming, the New task
// manual create action, and the pause/delete row actions dispatching the
// mutation hooks.
//
// The scheduled-tasks hooks are mocked at their seam; the create dialog is
// stubbed to a marker so we assert the page opens it without exercising its
// internals (covered by its own tests).

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EmptyState, TasksPage } from "./TasksPage";
import * as hooks from "@/hooks/useScheduledTasks";
import * as conversationHooks from "@/hooks/useConversations";
import { ScheduledTaskApiError, type ScheduledTask } from "@/lib/scheduledTasksApi";

vi.mock("@/hooks/useScheduledTasks", () => ({
  useScheduledTasks: vi.fn(),
  useUpdateScheduledTask: vi.fn(),
  useDeleteScheduledTask: vi.fn(),
  useRunScheduledTaskNow: vi.fn(),
}));
vi.mock("@/hooks/useConversations", () => ({ useProjects: vi.fn() }));

// Stub the create dialog — its internals are covered separately; here we only
// need to know it opened and WHICH prefill (initialName/initialPrompt) TasksPage
// passed, so we can assert chip-click seeds it and manual open does not.
vi.mock("@/components/scheduled/CreateScheduledTaskDialog", () => ({
  CreateScheduledTaskDialog: ({
    open,
    initialName,
    initialPrompt,
    initialProjectId,
    editingTask,
  }: {
    open: boolean;
    initialName?: string;
    initialPrompt?: string;
    initialProjectId?: string;
    editingTask?: ScheduledTask | null;
  }) =>
    open ? (
      <div
        data-testid="manual-dialog-open"
        data-initial-name={initialName ?? ""}
        data-initial-prompt={initialPrompt ?? ""}
        data-initial-project-id={initialProjectId ?? ""}
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
    workspace: null,
    hostId: null,
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

function setTasks(tasks: ScheduledTask[], state: { isLoading?: boolean; isError?: boolean } = {}) {
  vi.mocked(hooks.useScheduledTasks).mockReturnValue({
    data: tasks,
    isLoading: state.isLoading ?? false,
    isError: state.isError ?? false,
    refetch: vi.fn(),
    error: state.isError ? new Error("load failed") : null,
  } as unknown as ReturnType<typeof hooks.useScheduledTasks>);
}

interface QueryState {
  data?: ScheduledTask[];
  isLoading?: boolean;
  isError?: boolean;
  error?: Error | null;
  refetch?: ReturnType<typeof vi.fn>;
}

function setTaskQueries({
  all,
  project = all,
  unfiled = all,
}: {
  all: QueryState;
  project?: QueryState;
  unfiled?: QueryState;
}) {
  vi.mocked(hooks.useScheduledTasks).mockImplementation((filter = { kind: "all" }) => {
    const selected = filter.kind === "all" ? all : filter.kind === "unfiled" ? unfiled : project;
    return {
      data: selected.data,
      isLoading: selected.isLoading ?? false,
      isError: selected.isError ?? false,
      error: selected.error ?? null,
      refetch: selected.refetch ?? vi.fn(),
    } as unknown as ReturnType<typeof hooks.useScheduledTasks>;
  });
}

async function chooseProjectFilter(name: string) {
  fireEvent.keyDown(screen.getByTestId("tasks-project-filter"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("option", { name }));
}

beforeEach(() => {
  vi.clearAllMocks();
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
  vi.mocked(conversationHooks.useProjects).mockReturnValue({
    // Project A carries an emoji icon; Project B deliberately has none, so the
    // shared cases below cover both the emoji and the folder-fallback paths.
    data: [
      { id: "p_a", name: "Project A", icon: "📊" },
      { id: "p_b", name: "Project B" },
      { id: null, name: "Legacy only" },
    ],
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof conversationHooks.useProjects>);
});

afterEach(() => cleanup());

function renderPage() {
  return render(
    <MemoryRouter>
      <TasksPage />
    </MemoryRouter>,
  );
}

describe("EmptyState", () => {
  it("keeps the rich global layout when its display copy changes", () => {
    render(
      <EmptyState
        variant="global"
        message="Start your first automation"
        showSuggestions={false}
        onPickSuggestion={vi.fn()}
      />,
    );

    expect(screen.getByText("Start your first automation")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Create a task to run an agent session automatically on a recurring schedule.",
      ),
    ).toBeInTheDocument();
  });
});

describe("TasksPage list", () => {
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
        "Create a task to run an agent session automatically on a recurring schedule.",
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

describe("Project filtering", () => {
  it("mounts only one scheduled-task polling subscription", async () => {
    setTaskQueries({ all: { data: [task()] }, project: { data: [] } });
    renderPage();
    vi.mocked(hooks.useScheduledTasks).mockClear();

    await chooseProjectFilter("Project A");

    expect(hooks.useScheduledTasks).toHaveBeenCalledTimes(1);
    expect(hooks.useScheduledTasks).toHaveBeenLastCalledWith({
      kind: "project",
      projectId: "p_a",
    });
  });

  it("switches the subscription to the selected server slice", async () => {
    const inA = task({ id: "a", name: "In A", projectId: "p_a" });
    const inB = task({ id: "b", name: "In B", projectId: "p_b" });
    setTaskQueries({ all: { data: [inA, inB] }, project: { data: [inA] } });
    renderPage();

    await chooseProjectFilter("Project A");

    expect(hooks.useScheduledTasks).toHaveBeenCalledWith({ kind: "all" });
    expect(hooks.useScheduledTasks).toHaveBeenCalledWith({ kind: "project", projectId: "p_a" });
    expect(screen.getByText("In A")).toBeInTheDocument();
    expect(screen.queryByText("In B")).toBeNull();
    expect(screen.queryByTestId("task-project-chip")).toBeNull();
  });

  it("renders a nonempty Project slice when the cached All slice is empty", async () => {
    const inA = task({ id: "a", name: "First automation", projectId: "p_a" });
    setTaskQueries({ all: { data: [] }, project: { data: [inA] } });
    renderPage();

    await chooseProjectFilter("Project A");

    expect(screen.getByText("First automation")).toBeInTheDocument();
    expect(screen.queryByTestId("tasks-empty-state")).toBeNull();
  });

  it("uses narrowed Project copy when All is still loading", async () => {
    setTaskQueries({ all: { data: undefined, isLoading: true }, project: { data: [] } });
    renderPage();
    expect(screen.getByText("Loading automations…")).toBeInTheDocument();

    await chooseProjectFilter("Project A");

    expect(screen.getByText("No automations in Project A")).toBeInTheDocument();
    expect(screen.queryByText("No automations yet")).toBeNull();
  });

  it("prefills create from a selected first-class Project", async () => {
    setTaskQueries({ all: { data: [task()] }, project: { data: [] } });
    renderPage();
    await chooseProjectFilter("Project A");
    fireEvent.click(screen.getByTestId("new-task-button"));
    expect(screen.getByTestId("manual-dialog-open")).toHaveAttribute(
      "data-initial-project-id",
      "p_a",
    );
  });

  it("shows resolved chips in All and hides null or dangling assignments", () => {
    setTasks([
      task({ id: "a", projectId: "p_a" }),
      task({ id: "null", name: "Null", projectId: null }),
      task({ id: "dangling", name: "Dangling", projectId: "deleted" }),
    ]);
    renderPage();
    expect(screen.getAllByTestId("task-project-chip")).toHaveLength(1);
    expect(screen.getByTestId("task-project-chip")).toHaveTextContent("Project A");
    expect(
      screen.getByTestId("tasks-list").querySelectorAll('[data-testid="tasks-list"]'),
    ).toHaveLength(0);
  });

  it("uses the exact global, Project, Unfiled, and client-no-match precedence", async () => {
    setTaskQueries({ all: { data: [] }, project: { data: [] } });
    const { unmount } = renderPage();
    await chooseProjectFilter("Project A");
    expect(screen.getByText("No automations yet")).toBeInTheDocument();
    unmount();

    cleanup();
    setTaskQueries({ all: { data: [task()] }, project: { data: [] }, unfiled: { data: [] } });
    renderPage();
    await chooseProjectFilter("Project A");
    expect(screen.getByText("No automations in Project A")).toBeInTheDocument();
    await chooseProjectFilter("Unfiled");
    expect(screen.getByText("No unfiled automations")).toBeInTheDocument();

    cleanup();
    setTasks([task({ state: "active" })]);
    renderPage();
    fireEvent.click(screen.getByTestId("tasks-filter-paused"));
    expect(screen.getByText("No automations found")).toBeInTheDocument();
  });

  it("gives task error and loading states precedence only without cached data", () => {
    setTaskQueries({ all: { data: undefined, isError: true, error: new Error("boom") } });
    const { unmount } = renderPage();
    expect(screen.getByText("Couldn’t load automations.")).toBeInTheDocument();
    unmount();

    cleanup();
    setTaskQueries({ all: { data: undefined, isLoading: true } });
    renderPage();
    expect(screen.getByText("Loading automations…")).toBeInTheDocument();
  });

  it("keeps Project-list loading and errors inline without replacing task content", () => {
    setTasks([task()]);
    vi.mocked(conversationHooks.useProjects).mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof conversationHooks.useProjects>);
    const { rerender } = renderPage();
    expect(screen.getByTestId("tasks-project-filter")).toBeDisabled();
    expect(screen.getByText("Nightly triage")).toBeInTheDocument();

    vi.mocked(conversationHooks.useProjects).mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof conversationHooks.useProjects>);
    rerender(
      <MemoryRouter>
        <TasksPage />
      </MemoryRouter>,
    );
    expect(screen.getByText(/Couldn't load projects/)).toBeInTheDocument();
    expect(screen.getByText("Nightly triage")).toBeInTheDocument();
  });

  it("hides Suggestions for Project, Unfiled, search, and status filters", async () => {
    setTaskQueries({
      all: { data: [task()] },
      project: { data: [task()] },
      unfiled: { data: [task()] },
    });
    renderPage();
    expect(screen.getByTestId("tasks-suggestions")).toBeInTheDocument();
    await chooseProjectFilter("Project A");
    expect(screen.queryByTestId("tasks-suggestions")).toBeNull();
    await chooseProjectFilter("All projects");
    fireEvent.change(screen.getByTestId("tasks-search"), { target: { value: "night" } });
    expect(screen.queryByTestId("tasks-suggestions")).toBeNull();
    fireEvent.change(screen.getByTestId("tasks-search"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("tasks-filter-active"));
    expect(screen.queryByTestId("tasks-suggestions")).toBeNull();
    fireEvent.click(screen.getByTestId("tasks-filter-all"));
    await chooseProjectFilter("Unfiled");
    expect(screen.queryByTestId("tasks-suggestions")).toBeNull();
  });

  it("resets a deleted selection and a transient 404 to All", async () => {
    const allRefetch = vi.fn();
    setTaskQueries({ all: { data: [task()], refetch: allRefetch }, project: { data: [] } });
    const { rerender } = renderPage();
    await chooseProjectFilter("Project A");
    vi.mocked(conversationHooks.useProjects).mockReturnValue({
      data: [{ id: "p_b", name: "Project B" }],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof conversationHooks.useProjects>);
    rerender(
      <MemoryRouter>
        <TasksPage />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("tasks-project-filter")).toHaveTextContent("All projects"),
    );

    vi.mocked(conversationHooks.useProjects).mockReturnValue({
      data: [{ id: "p_a", name: "Project A" }],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof conversationHooks.useProjects>);
    setTaskQueries({
      all: { data: [task()], refetch: allRefetch },
      project: {
        data: undefined,
        isError: true,
        error: new ScheduledTaskApiError("Project not found", 404, "not_found"),
      },
    });
    rerender(
      <MemoryRouter>
        <TasksPage />
      </MemoryRouter>,
    );
    await chooseProjectFilter("Project A");
    await waitFor(() =>
      expect(screen.getByTestId("tasks-project-filter")).toHaveTextContent("All projects"),
    );
    expect(allRefetch).toHaveBeenCalled();
  });
});

describe("Project emoji icons", () => {
  it("shows each project's emoji (or the folder fallback) in the filter options", async () => {
    setTasks([task()]);
    renderPage();
    fireEvent.keyDown(screen.getByTestId("tasks-project-filter"), { key: "Enter" });
    // The emoji is aria-hidden, so the option's accessible NAME stays the plain
    // project name — the same selector the rest of the suite relies on.
    const optionA = await screen.findByRole("option", { name: "Project A" });
    expect(within(optionA).getByTestId("project-label-icon")).toHaveTextContent("📊");
    const optionB = screen.getByRole("option", { name: "Project B" });
    expect(within(optionB).getByTestId("project-label-fallback")).toBeInTheDocument();
    expect(within(optionB).queryByTestId("project-label-icon")).toBeNull();
  });

  it("shows the selected project's emoji in the filter trigger", async () => {
    setTaskQueries({ all: { data: [task()] }, project: { data: [] } });
    renderPage();
    await chooseProjectFilter("Project A");
    const trigger = screen.getByTestId("tasks-project-filter");
    expect(trigger).toHaveTextContent("📊");
    expect(trigger).toHaveTextContent("Project A");
  });

  it("shows the project emoji in the row chip", () => {
    setTasks([task({ id: "a", projectId: "p_a" })]);
    renderPage();
    const chip = screen.getByTestId("task-project-chip");
    expect(within(chip).getByTestId("project-label-icon")).toHaveTextContent("📊");
    expect(chip).toHaveTextContent("Project A");
  });

  it("renders the folder fallback (not a blank gap) for an emoji-less project's chip", () => {
    setTasks([task({ id: "b", projectId: "p_b" })]);
    renderPage();
    const chip = screen.getByTestId("task-project-chip");
    expect(within(chip).getByTestId("project-label-fallback")).toBeInTheDocument();
    expect(within(chip).queryByTestId("project-label-icon")).toBeNull();
    expect(chip).toHaveTextContent("Project B");
  });
});

describe("New task button", () => {
  it("opens the manual create dialog directly (no dropdown)", () => {
    setTasks([]);
    renderPage();
    fireEvent.click(screen.getByTestId("new-task-button"));
    expect(screen.getByTestId("manual-dialog-open")).toBeInTheDocument();
  });

  it("does not offer a 'Create with Omnigent' entry point", () => {
    setTasks([]);
    renderPage();
    // No dropdown and no deferred create option: the manual dialog is the only
    // create path on this page.
    fireEvent.pointerDown(screen.getByTestId("new-task-button"), { button: 0 });
    expect(screen.queryByTestId("new-task-omnigent")).toBeNull();
    expect(screen.queryByTestId("new-task-manual")).toBeNull();
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

  it("opens EMPTY from the plain 'New task' button (no prefill)", () => {
    setTasks([]);
    renderPage();
    fireEvent.click(screen.getByTestId("new-task-button"));
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
