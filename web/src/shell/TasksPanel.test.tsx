import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type * as UseSavedWorkflowsModule from "@/hooks/useSavedWorkflows";
import type * as UseSessionJournalModule from "@/hooks/useSessionJournal";
import type * as SidebarDataModule from "@/hooks/useSidebarData";
import type * as CapabilitiesModule from "@/lib/CapabilitiesContext";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FullscreenIcon } from "@/components/ai-elements/MermaidBlock";
import { type ChildSessionInfo, useChildSessions } from "@/hooks/useChildSessions";
import { useSavedWorkflows } from "@/hooks/useSavedWorkflows";
import { type SessionJournalView, useSessionJournal } from "@/hooks/useSessionJournal";
import { useOptionalLoadedConversations } from "@/hooks/useSidebarData";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { formatMessageTimestamp } from "@/lib/relativeTime";
import type { JournalEntry, StepProgress } from "@/lib/sessionJournal";
import type { ConversationState } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import {
  openTasksTab,
  registerContinuePlan,
  registerSaveWorkflow,
  registerStartWorkflow,
  registerStopWork,
  registerTaskAction,
  useSessionTasksActions,
} from "@/store/sessionTasksActions";
import { journalEntry, journalPlan } from "@/test/journalFixtures";
import { conversation, conversationPage } from "@/test/sidebarMockHelpers";
import { TasksPanel, type TaskMenuAction } from "./TasksPanel";

// The graph has its own tests (React Flow runs out of memory in jsdom); here the
// full-screen view lists the steps it draws, where each task stands, and its ⋯ action.
vi.mock("@/components/workflows/WorkflowStepsGraph", () => ({
  WorkflowGraphOverlay: (props: {
    title: string;
    steps: readonly { id: string; depends_on: string[] }[];
    progress?: StepProgress[];
    menuActionFor?: (item: StepProgress) => TaskMenuAction | undefined;
    onClose: () => void;
  }) => (
    <div
      data-testid="workflow-graph-overlay"
      data-title={props.title}
      data-steps={props.steps.map((step) => `${step.id}<${step.depends_on.join("+")}`).join(" ")}
    >
      {props.progress?.map((item) => `${item.step.id}:${item.state}`).join(" ")}
      {props.progress?.map((item) => {
        const menuAction = props.menuActionFor?.(item);
        return (
          menuAction && (
            <button key={item.step.id} type="button" onClick={menuAction.onSelect}>
              {`${menuAction.action} ${item.step.id}`}
            </button>
          )
        );
      })}
      <button type="button" onClick={props.onClose}>
        Close graph
      </button>
    </div>
  ),
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: vi.fn(),
}));
vi.mock("@/hooks/useSavedWorkflows", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSavedWorkflowsModule>()),
  useSavedWorkflows: vi.fn(),
}));
vi.mock("@/hooks/useSessionJournal", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionJournalModule>()),
  useSessionJournal: vi.fn(),
}));
vi.mock("@/hooks/useSidebarData", async (importOriginal) => ({
  ...(await importOriginal<typeof SidebarDataModule>()),
  useOptionalLoadedConversations: vi.fn(),
}));
vi.mock("@/lib/CapabilitiesContext", async (importOriginal) => ({
  ...(await importOriginal<typeof CapabilitiesModule>()),
  useServerInfo: vi.fn(),
}));

const RUN = "0b6c5f0d8a7e4c3b9a1d2e3f4a5b6c7d";

function showJournal(entries: JournalEntry[], extra = {}) {
  const view: SessionJournalView = {
    entries,
    isLoading: false,
    error: null,
    hasOlder: false,
    loadingOlder: false,
    loadOlder: vi.fn(),
    retry: vi.fn(),
    ...extra,
  };
  vi.mocked(useSessionJournal).mockReturnValue(view);
  return view;
}

function status(position: number, step: string, value: string, text?: string, run?: string) {
  return journalEntry(position, {
    kind: "status",
    step,
    scheduled_task_run_id: run ?? null,
    content: { status: value, text: text ?? null },
  });
}

/** Shows the URL's search string, where a task's click asks the chat for its marker. */
function SearchProbe() {
  const location = useLocation();
  return (
    <output data-testid="search" data-pathname={location.pathname}>
      {location.search}
    </output>
  );
}

function renderPanel(
  onStartWorkflow?: () => void,
  onShowInChat?: () => void,
  initialEntry = "/c/conv",
) {
  return render(
    <>
      <TasksPanel
        conversationId="conv"
        active
        onStartWorkflow={onStartWorkflow}
        onShowInChat={onShowInChat}
      />
      <SearchProbe />
    </>,
    {
      wrapper: ({ children }) => (
        <MemoryRouter initialEntries={[initialEntry]}>{children}</MemoryRouter>
      ),
    },
  );
}

/** The session's live state: whether it works, and the agent's own task list. */
function setSession(state: Partial<ConversationState>) {
  act(() => conversationRegistry.acquire("conv").setState(state));
}

beforeEach(() => {
  vi.mocked(useOptionalLoadedConversations).mockReturnValue(undefined);
  vi.mocked(useChildSessions).mockReturnValue({ children: [] } as never);
  vi.mocked(useServerInfo).mockReturnValue({
    session_journal: true,
    features: { workflows: true },
  } as never);
  vi.mocked(useSavedWorkflows).mockReturnValue({
    workflows: [],
    isLoading: false,
    isError: false,
    refresh: vi.fn(),
    pagination: { hasMore: false, loadingMore: false, loadMore: vi.fn() },
  });
  conversationRegistry
    .acquire("conv")
    .setState({ sessionStatus: "idle", todos: [], backgroundTaskCount: 0 });
});

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
});

describe("TasksPanel", () => {
  it("says where tasks come from, and links to starting a workflow when the composer can", () => {
    showJournal([]);
    renderPanel();
    expect(screen.getByText("No tasks yet")).toBeTruthy();
    expect(screen.getByText("Tasks show up here when the agent makes a to-do list.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "run a workflow" })).toBeNull();
    cleanup();

    const start = vi.fn();
    const closeDrawer = vi.fn();
    const withdraw = registerStartWorkflow("conv", start);
    try {
      renderPanel(closeDrawer);
      expect(screen.getByText(/to-do list or you/).textContent).toBe(
        "Tasks show up here when the agent makes a to-do list or you run a workflow.",
      );
      fireEvent.click(screen.getByRole("button", { name: "run a workflow" }));
      expect(closeDrawer).toHaveBeenCalled();
      expect(start).toHaveBeenCalled();
    } finally {
      act(withdraw);
    }
  });

  it("shows the agent's own task list when the session has no plan", () => {
    showJournal([]);
    setSession({
      sessionStatus: "running",
      blocks: [
        {
          type: "native_tool",
          ctx: {
            agent: null,
            depth: 0,
            turn: 0,
            timestamp: 0,
            responseId: "turn",
            itemId: "marker",
          },
          toolType: "subagent_activity",
          label: "Started Document it",
          data: {
            type: "resource_event",
            event_type: "session.journal.task",
            resource_type: "journal",
            resource_id: "task-2",
            resource: { name: "Document it", status: "started", native: true },
          },
        },
      ],
      todos: [
        { content: "Add the flag", status: "completed", activeForm: "Adding the flag" },
        { content: "Document it", status: "in_progress", activeForm: "Documenting it" },
        { content: "Run the tests", status: "pending", activeForm: "Running the tests" },
      ],
    });
    renderPanel();
    const rows = screen.getAllByTestId("tasks-row");
    expect(rows.map((row) => row.dataset.state)).toEqual(["done", "running", "waiting"]);
    expect(rows[1].textContent).toContain("Documenting it");
    // Their rows have no history to open, and nothing waits on another. A
    // started one links to its marker in the chat; one not started yet doesn't.
    expect(within(rows[1]).queryByTestId("tasks-row-history")).toBeNull();
    expect(within(rows[1]).getByTestId("tasks-row-show-in-chat")).toBeTruthy();
    expect(within(rows[2]).queryByRole("button")).toBeNull();
    // The list says nothing about what waits on what, so its strip's gaps mean nothing either.
    expect(screen.getByTestId("tasks-progress").dataset.links).toBeUndefined();
    expect(within(rows[2]).getByRole("img", { name: "Not started" }).textContent).toBe("3");
    expect(screen.getByTestId("tasks-summary").textContent).toBe("1 of 3 finished");
    expect(screen.queryByTestId("tasks-plan-name")).toBeNull();

    setSession({ sessionStatus: "idle" });
    expect(screen.getAllByTestId("tasks-row")[1].dataset.state).toBe("stopped");
  });

  it("shows the plan instead of the agent's own list once the session has one", () => {
    showJournal([journalPlan(0, ["profile"], ["ship", ["profile"]])]);
    setSession({ todos: [{ content: "Scratch item", status: "pending", activeForm: "" }] });
    renderPanel();
    expect(screen.getAllByTestId("tasks-row").map((row) => row.dataset.stepId)).toEqual([
      "profile",
      "ship",
    ]);
    expect(screen.queryByText("Scratch item")).toBeNull();
  });

  it.each([true, false, undefined])(
    "preserves the agent's own list without a journal on workflows=%s",
    (workflows) => {
      vi.mocked(useServerInfo).mockReturnValue({
        session_journal: false,
        features: workflows === undefined ? {} : { workflows },
      } as never);
      showJournal([journalPlan(0, ["profile"])]);
      setSession({
        todos: [
          { content: "Only item", status: "pending", activeForm: "" },
          { content: "Started item", status: "in_progress", activeForm: "Working" },
          { content: "Finished item", status: "completed", activeForm: "" },
        ],
      });
      renderPanel();
      expect(screen.getAllByTestId("tasks-row")[0]).toHaveTextContent("Only item");
      expect(screen.queryAllByTestId("tasks-row-show-in-chat")).toHaveLength(0);
      expect(vi.mocked(useSessionJournal)).toHaveBeenLastCalledWith("conv", false);
    },
  );

  it("lists the plan's steps in order, each once, with where it stands", () => {
    showJournal([
      journalPlan(
        0,
        ["baseline"],
        ["batch", ["baseline"]],
        ["pool", ["batch"]],
        ["tests", ["pool"]],
        ["ship", ["tests"]],
      ),
      status(1, "baseline", "started"),
      status(2, "baseline", "done", "p95 434 ms"),
      status(3, "batch", "started"),
      status(4, "batch", "done", "Kept: -9%"),
      status(5, "pool", "failed", "Reverted"),
      status(6, "tests", "needs_input", "The test database is down: wait, or skip it?"),
    ]);
    renderPanel();
    const steps = screen.getAllByTestId("tasks-row");
    expect(steps.map((step) => step.dataset.state)).toEqual([
      "done",
      "done",
      "failed",
      "needs_input",
      "waiting",
    ]);
    expect(steps[0].textContent).toContain("p95 434 ms");
    expect(steps[3].textContent).toContain("Waiting for you");
    expect(steps[3].textContent).toContain("The test database is down: wait, or skip it?");
    expect(within(steps[4]).getByRole("img", { name: "Not started" }).textContent).toBe("5");
    // The failed try counts as finished; the count beside the summary names it.
    expect(screen.getByTestId("tasks-summary").textContent).toBe("3 of 5 finished");
    expect(screen.getByText("1 failed")).toBeTruthy();
  });

  it("marks every step that can start as ready, and shows on the strip what waits on what", () => {
    showJournal([
      journalPlan(
        0,
        ["profile"],
        ["batch", ["profile"]],
        ["pool", ["profile"]],
        ["ship", ["batch", "pool"]],
      ),
      status(1, "profile", "done"),
    ]);
    renderPanel();
    const steps = screen.getAllByTestId("tasks-row");
    expect(steps.map((step) => step.dataset.state)).toEqual(["done", "ready", "ready", "waiting"]);
    expect(within(steps[1]).getByRole("img", { name: "Ready" }).textContent).toBe("2");
    expect(within(steps[1]).getByText("Ready")).toBeTruthy();
    expect(screen.getByTestId("tasks-progress").dataset.links).toBe("true");
    const segments = screen.getAllByTestId("tasks-progress-segment");
    // Hovering batch arcs it to profile, which it waits on, and ship, which waits on it.
    fireEvent.pointerEnter(segments[1]);
    expect(screen.getAllByTestId("tasks-progress-arc")).toHaveLength(2);
    expect(segments.map((segment) => segment.dataset.dimmed === "true")).toEqual([
      false,
      false,
      true,
      false,
    ]);
    fireEvent.pointerLeave(screen.getByTestId("tasks-progress"));
    expect(screen.queryAllByTestId("tasks-progress-arc")).toHaveLength(0);
    expect(segments.some((segment) => segment.dataset.dimmed)).toBe(false);
  });

  it("marks a plan that runs or was saved as a workflow, naming it on hover", async () => {
    const workflowId = "0123456789abcdef0123456789abcdef";
    vi.mocked(useSavedWorkflows).mockReturnValue({
      workflows: [{ id: workflowId, name: "Triage flaky tests", description: "Finds them." }],
      isLoading: false,
      isError: false,
      refresh: vi.fn(),
      pagination: { hasMore: false, loadingMore: false, loadMore: vi.fn() },
    });
    showJournal([{ ...journalPlan(0, ["explore"]), workflow_id: workflowId }]);
    renderPanel();
    const chip = screen.getByTestId("tasks-plan-workflow");
    expect(chip).toHaveTextContent("Workflow");
    expect(chip).toHaveAttribute("href", `/workflows/${workflowId}`);
    fireEvent.focus(chip);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "Saved workflow “Triage flaky tests”",
    );
    cleanup();

    // A replan names no workflow, but it keeps the run's step, so the chip stays.
    showJournal([
      { ...journalPlan(0, ["explore"]), workflow_id: workflowId },
      journalPlan(1, ["explore"], ["fix", ["explore"]]),
    ]);
    renderPanel();
    expect(screen.getByTestId("tasks-plan-workflow")).toHaveAttribute(
      "href",
      `/workflows/${workflowId}`,
    );
    cleanup();

    showJournal([journalPlan(0, ["explore"])]);
    renderPanel();
    expect(screen.getByTestId("tasks-plan-name")).toHaveTextContent("The plan");
    expect(screen.queryByTestId("tasks-plan-workflow")).toBeNull();
  });

  it("shows the workflow runs below a workflow's run after its tasks", () => {
    const workflowId = "0123456789abcdef0123456789abcdef";
    vi.mocked(useSavedWorkflows).mockReturnValue({
      workflows: [],
      isLoading: false,
      isError: false,
      refresh: vi.fn(),
      pagination: { hasMore: false, loadingMore: false, loadMore: vi.fn() },
    });
    const renderWorkflowRuns = vi.fn((id: string) => <p data-testid="workflow-runs-slot">{id}</p>);
    const renderTasks = (active = true) =>
      render(
        <TasksPanel
          conversationId="conv"
          active={active}
          renderWorkflowRuns={renderWorkflowRuns}
        />,
        { wrapper: ({ children }) => <MemoryRouter>{children}</MemoryRouter> },
      );
    showJournal([{ ...journalPlan(0, ["explore"]), workflow_id: workflowId }]);
    // A hidden panel leaves the runs out, so their list doesn't keep refreshing.
    renderTasks(false);
    expect(renderWorkflowRuns).not.toHaveBeenCalled();
    cleanup();
    renderTasks();
    const slot = screen.getByTestId("workflow-runs-slot");
    expect(slot).toHaveTextContent(workflowId);
    const lastTask = screen.getAllByTestId("tasks-row").at(-1)!;
    expect(lastTask.compareDocumentPosition(slot) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    cleanup();

    // A plan of the agent's own names no workflow, so it has no runs to list.
    renderWorkflowRuns.mockClear();
    showJournal([journalPlan(0, ["explore"])]);
    renderTasks();
    expect(renderWorkflowRuns).not.toHaveBeenCalled();
  });

  it("says on hover which task a progress segment is and where it stands", async () => {
    showJournal([
      journalPlan(0, ["profile"], ["cache", ["profile"]], ["ship", ["cache"]]),
      status(1, "profile", "done"),
    ]);
    renderPanel();
    const segments = screen.getAllByTestId("tasks-progress-segment");
    fireEvent.focus(segments[1]);
    const ready = await screen.findByTestId("tasks-progress-card");
    expect(ready).toHaveTextContent("Task 2 of 3");
    expect(ready).toHaveTextContent("cache");
    expect(ready).toHaveTextContent("Ready to start");
    fireEvent.blur(segments[1]);
    fireEvent.focus(segments[2]);
    await waitFor(() =>
      expect(screen.getByTestId("tasks-progress-card")).toHaveTextContent("Waiting on cache"),
    );
  });

  it("says a task can't start when a task it waits on failed, on its row and its card", async () => {
    showJournal([
      journalPlan(0, ["fix"], ["suite", ["fix"]], ["pr", ["suite"]]),
      status(1, "fix", "failed", "Couldn't reproduce the race"),
    ]);
    renderPanel();
    const steps = screen.getAllByTestId("tasks-row");
    expect(steps.map((step) => step.dataset.state)).toEqual(["failed", "waiting", "waiting"]);
    // pr waits on fix through suite, and names fix too.
    expect(steps[1]).toHaveTextContent("Can't start · fix failed");
    expect(steps[2]).toHaveTextContent("Can't start · fix failed");
    const segments = screen.getAllByTestId("tasks-progress-segment");
    expect(segments[2].getAttribute("aria-label")).toBe("Task 3, pr: Can't start");
    fireEvent.focus(segments[2]);
    expect(await screen.findByTestId("tasks-progress-card")).toHaveTextContent(
      "Can't start · fix failed",
    );
  });

  it("offers a task's ⋯ menu on its segment's card, and keeps the card up while it's open", async () => {
    showJournal([
      journalPlan(0, ["build"], ["deploy", ["build"]]),
      status(1, "build", "done"),
      status(2, "deploy", "failed", "The tests failed"),
    ]);
    const taskAction = vi.fn();
    const withdraw = registerTaskAction("conv", taskAction);
    try {
      renderPanel();
      const [built, failed] = screen.getAllByTestId("tasks-progress-segment");
      // A done task has nothing to offer.
      fireEvent.focus(built!);
      expect(await screen.findByTestId("tasks-progress-card")).toHaveTextContent("build");
      expect(screen.queryByTestId("tasks-progress-menu")).toBeNull();
      fireEvent.blur(built!);
      fireEvent.focus(failed!);
      await waitFor(() =>
        expect(screen.getByTestId("tasks-progress-card")).toHaveTextContent("The tests failed"),
      );
      fireEvent.pointerDown(screen.getByTestId("tasks-progress-menu"), { button: 0 });
      fireEvent.pointerUp(document);
      // Leaving the segment would close the card, but not while its menu is open.
      fireEvent.blur(failed!);
      await new Promise((resolve) => {
        setTimeout(resolve, 200);
      });
      expect(screen.getByTestId("tasks-progress-card")).toBeInTheDocument();
      expect(screen.getByTestId("tasks-progress-retry")).toHaveTextContent("Retry");
      fireEvent.click(screen.getByTestId("tasks-progress-retry"));
      expect(taskAction).toHaveBeenLastCalledWith("retry", "deploy");
      // Picking the action puts the card away.
      await waitFor(() => expect(screen.queryByTestId("tasks-progress-card")).toBeNull());
    } finally {
      act(withdraw);
    }
  });

  it("jumps to a task when its progress segment is clicked", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      showJournal([journalPlan(0, ["a"], ["b"], ["c"])]);
      renderPanel();
      fireEvent.click(screen.getAllByTestId("tasks-progress-segment")[2]);
      const row = screen.getAllByTestId("tasks-row")[2];
      expect(scrollIntoView.mock.contexts[0]).toBe(row);
      expect(row.dataset.flash).toBe("true");
    } finally {
      delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    }
  });

  it.each(["/c/conv", "/grid"])(
    "takes a started task from %s to its newest chat marker; one not started doesn't link",
    (initialEntry) => {
      const onShowInChat = vi.fn();
      showJournal([
        journalPlan(0, ["deploy"], ["verify", ["deploy"]], ["video", ["verify"]]),
        status(1, "deploy", "started"),
        status(2, "deploy", "done", "Pod 1/1"),
        status(3, "verify", "started"),
      ]);
      renderPanel(undefined, onShowInChat, initialEntry);
      const [deploy, verify, video] = screen.getAllByTestId("tasks-row");
      // A finished task goes to where it ended, a running one to its newest start.
      for (const [row, marker] of [
        [deploy, "journal-e2"],
        [verify, "journal-e3"],
      ] as const) {
        fireEvent.click(within(row).getByTestId("tasks-row-show-in-chat"));
        const search = new URLSearchParams(screen.getByTestId("search").textContent ?? "");
        expect(search.get("message")).toBe(marker);
        expect(search.get("message_request")).toBeTruthy();
        expect(screen.getByTestId("search")).toHaveAttribute("data-pathname", "/c/conv");
      }
      expect(onShowInChat).toHaveBeenCalledTimes(2);
      expect(within(video).queryByTestId("tasks-row-show-in-chat")).toBeNull();
      // Its history opens from the chevron instead.
      expect(within(video).getByTestId("tasks-row-history")).toBeTruthy();
    },
  );

  it("scrolls to and highlights the task a chat marker asks for", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      showJournal([journalPlan(0, ["a"], ["b"], ["c"])]);
      renderPanel();
      act(() => openTasksTab("conv", "c"));
      const row = screen.getAllByTestId("tasks-row")[2];
      await waitFor(() => expect(row.dataset.flash).toBe("true"));
      expect(scrollIntoView.mock.contexts[0]).toBe(row);
      expect(useSessionTasksActions.getState().taskToShow).toBeNull();
    } finally {
      delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    }
  });

  it("offers Save as workflow in the plan's menu once the composer can save", async () => {
    const named = journalPlan(0, ["baseline"]);
    named.content.name = "Cut GET /orders p95 by 20%";
    showJournal([named]);
    renderPanel();
    expect(screen.queryByTestId("tasks-plan-menu")).toBeNull();
    cleanup();

    const save = vi.fn();
    const withdraw = registerSaveWorkflow("conv", save);
    try {
      renderPanel();
      fireEvent.pointerDown(screen.getByTestId("tasks-plan-menu"), { button: 0 });
      fireEvent.click(screen.getByTestId("tasks-save-workflow"));
      await waitFor(() => expect(save).toHaveBeenCalledWith("Cut GET /orders p95 by 20%"));
    } finally {
      act(withdraw);
    }
  });

  it.each([false, true])(
    "disables saving an existing workflow and explains where to edit it (replanned: %s)",
    async (replanned) => {
      const workflowId = "0123456789abcdef0123456789abcdef";
      showJournal([
        { ...journalPlan(0, ["explore"]), workflow_id: workflowId },
        ...(replanned ? [journalPlan(1, ["explore"], ["fix", ["explore"]])] : []),
      ]);
      const save = vi.fn();
      const withdraw = registerSaveWorkflow("conv", save);
      const user = userEvent.setup();
      try {
        renderPanel();
        await user.click(screen.getByRole("button", { name: "Plan actions" }));
        const item = screen.getByRole("menuitem", { name: "Save as workflow…" });
        expect(item).toHaveAttribute("aria-disabled", "true");
        expect(item).toHaveAccessibleDescription(
          "Already saved as a workflow. Open the workflow to edit it.",
        );
        await user.hover(item);
        expect(await screen.findByRole("tooltip")).toHaveTextContent(
          "Already saved as a workflow. Open the workflow to edit it.",
        );

        await user.click(item);
        await user.keyboard("{Enter} ");
        expect(screen.getByRole("menu")).toBeInTheDocument();
        await user.keyboard("{Escape}");
        await act(
          () =>
            new Promise<void>((resolve) => {
              requestAnimationFrame(() => resolve());
            }),
        );
        expect(save).not.toHaveBeenCalled();
      } finally {
        act(withdraw);
      }
    },
  );

  it("offers Run on a ready task, Continue on a stopped one or one waiting for you, and Retry on a failed one", () => {
    showJournal([
      journalPlan(0, ["baseline"], ["ship"], ["docs", ["ship"]], ["approve"], ["deploy"]),
      status(1, "baseline", "started"),
      status(2, "approve", "needs_input", "Deploy to prod now?"),
      status(3, "deploy", "failed", "The tests failed"),
    ]);
    renderPanel();
    expect(screen.queryByTestId("tasks-row-menu")).toBeNull();
    cleanup();

    const taskAction = vi.fn();
    const withdraw = registerTaskAction("conv", taskAction);
    try {
      renderPanel();
      const [stopped, ready, waiting, needsYou, failed] = screen.getAllByTestId("tasks-row");
      fireEvent.pointerDown(within(stopped!).getByTestId("tasks-row-menu"), { button: 0 });
      fireEvent.click(screen.getByTestId("tasks-row-continue"));
      expect(taskAction).toHaveBeenLastCalledWith("continue", "baseline");
      fireEvent.pointerDown(within(ready!).getByTestId("tasks-row-menu"), { button: 0 });
      fireEvent.click(screen.getByTestId("tasks-row-run"));
      expect(taskAction).toHaveBeenLastCalledWith("run", "ship");
      // A task still waiting on another has nothing to offer.
      expect(within(waiting!).queryByTestId("tasks-row-menu")).toBeNull();
      // Continue on a task waiting for you lets the agent go ahead with it.
      fireEvent.pointerDown(within(needsYou!).getByTestId("tasks-row-menu"), { button: 0 });
      expect(screen.getByTestId("tasks-row-proceed")).toHaveTextContent("Continue");
      fireEvent.click(screen.getByTestId("tasks-row-proceed"));
      expect(taskAction).toHaveBeenLastCalledWith("proceed", "approve");
      fireEvent.pointerDown(within(failed!).getByTestId("tasks-row-menu"), { button: 0 });
      expect(screen.getByTestId("tasks-row-retry")).toHaveTextContent("Retry");
      fireEvent.click(screen.getByTestId("tasks-row-retry"));
      expect(taskAction).toHaveBeenLastCalledWith("retry", "deploy");

      // Once the agent works on the started task again, it has nothing to continue.
      setSession({ sessionStatus: "running" });
      const [running] = screen.getAllByTestId("tasks-row");
      expect(within(running!).queryByTestId("tasks-row-menu")).toBeNull();
    } finally {
      act(withdraw);
    }
  });

  it("takes Continue to the question a waiting task's agent waits on, rather than messaging it", () => {
    showJournal([
      journalPlan(0, ["approve"], ["docs"]),
      status(1, "approve", "needs_input", "Deploy to prod now?"),
      status(2, "docs", "needs_input", "Paused at your request"),
    ]);
    const question = {
      type: "elicitation",
      elicitationId: "elicit_approve",
      status: "pending",
      phase: "workflow_request_input",
      journalStep: "approve",
    };
    setSession({ blocks: [question] as unknown as ConversationState["blocks"] });
    const taskAction = vi.fn();
    const onShowInChat = vi.fn();
    const withdraw = registerTaskAction("conv", taskAction);
    try {
      renderPanel(undefined, onShowInChat);
      const [approve, docs] = screen.getAllByTestId("tasks-row");
      fireEvent.pointerDown(within(approve!).getByTestId("tasks-row-menu"), { button: 0 });
      fireEvent.click(screen.getByTestId("tasks-row-proceed"));
      expect(taskAction).not.toHaveBeenCalled();
      expect(onShowInChat).toHaveBeenCalledOnce();
      const search = new URLSearchParams(screen.getByTestId("search").textContent ?? "");
      expect(search.get("message")).toBe("elicit_approve");

      // A task with no question waiting still asks the agent to go ahead.
      fireEvent.pointerDown(within(docs!).getByTestId("tasks-row-menu"), { button: 0 });
      fireEvent.click(screen.getByTestId("tasks-row-proceed"));
      expect(taskAction).toHaveBeenCalledWith("proceed", "docs");
    } finally {
      act(withdraw);
    }
  });

  it("offers Stop on a running task while the composer can stop the agent", () => {
    showJournal([journalPlan(0, ["baseline"]), status(1, "baseline", "started")]);
    setSession({ sessionStatus: "running" });
    renderPanel();
    expect(screen.queryByTestId("tasks-row-menu")).toBeNull();
    cleanup();

    const stop = vi.fn();
    const withdraw = registerStopWork("conv", stop);
    try {
      renderPanel();
      fireEvent.pointerDown(screen.getByTestId("tasks-row-menu"), { button: 0 });
      fireEvent.click(screen.getByTestId("tasks-row-stop"));
      expect(stop).toHaveBeenCalledTimes(1);
    } finally {
      act(withdraw);
    }
  });

  it("offers play in the header once the agent stopped with tasks left, and stop while it works", () => {
    const named = journalPlan(0, ["baseline"], ["ship", ["baseline"]]);
    named.content.name = "Ship the fix";
    showJournal([named, status(1, "baseline", "started")]);
    setSession({ sessionStatus: "idle" });
    // Without the composer's action, a viewer gets no play button.
    renderPanel();
    expect(screen.queryByTestId("plan-continue")).toBeNull();
    cleanup();

    const continuePlan = vi.fn();
    const stop = vi.fn();
    const withdrawContinue = registerContinuePlan("conv", continuePlan);
    try {
      renderPanel();
      fireEvent.click(screen.getByTestId("plan-continue"));
      expect(continuePlan).toHaveBeenCalledWith("Ship the fix");
      expect(screen.queryByTestId("plan-stop")).toBeNull();
      cleanup();

      setSession({ sessionStatus: "running" });
      const withdrawStop = registerStopWork("conv", stop);
      try {
        renderPanel();
        expect(screen.queryByTestId("plan-continue")).toBeNull();
        fireEvent.click(screen.getByTestId("plan-stop"));
        expect(stop).toHaveBeenCalledTimes(1);
      } finally {
        act(withdrawStop);
      }
    } finally {
      act(withdrawContinue);
    }
  });

  it("hides run controls after the final step finishes, even with unfinished earlier steps", () => {
    showJournal([
      journalPlan(0, ["explore"], ["work", ["explore"]], ["report", ["work"]]),
      status(1, "explore", "done"),
      status(2, "report", "done"),
    ]);
    const withdrawContinue = registerContinuePlan("conv", vi.fn());
    const withdrawStop = registerStopWork("conv", vi.fn());
    try {
      setSession({ sessionStatus: "running" });
      renderPanel();
      expect(screen.queryByTestId("plan-stop")).toBeNull();
      expect(screen.queryByTestId("plan-continue")).toBeNull();

      setSession({ sessionStatus: "idle" });
      expect(screen.queryByTestId("plan-stop")).toBeNull();
      expect(screen.queryByTestId("plan-continue")).toBeNull();
      expect(screen.queryByTestId("tasks-paused")).toBeNull();

      showJournal([
        journalPlan(0, ["explore"], ["work", ["explore"]], ["report", ["work"]]),
        status(1, "explore", "done"),
        status(2, "report", "done"),
        status(3, "work", "started"),
      ]);
      setSession({ sessionStatus: "running" });
      expect(screen.getByTestId("plan-stop")).toBeInTheDocument();
      setSession({ sessionStatus: "idle" });
      expect(screen.getByTestId("plan-continue")).toBeInTheDocument();

      showJournal([
        journalPlan(0, ["explore"], ["work", ["explore"]], ["report", ["work"]]),
        status(1, "explore", "done"),
        status(2, "report", "done"),
        journalPlan(3, ["explore"], ["review", ["explore"]], ["report", ["review"]]),
      ]);
      setSession({ sessionStatus: "running" });
      expect(screen.getByTestId("plan-stop")).toBeInTheDocument();
      setSession({ sessionStatus: "idle" });
      expect(screen.getByTestId("plan-continue")).toBeInTheDocument();

      const dependencyEntries = [
        journalPlan(0, ["review"], ["report"]),
        status(1, "review", "started"),
        status(2, "report", "done"),
        journalPlan(3, ["review"], ["report", ["review"]]),
      ];
      showJournal(dependencyEntries);
      setSession({ sessionStatus: "running" });
      expect(screen.getByTestId("plan-stop")).toBeInTheDocument();
      setSession({ sessionStatus: "idle" });
      expect(screen.getByTestId("plan-continue")).toBeInTheDocument();
      showJournal([...dependencyEntries, status(4, "review", "done"), status(5, "report", "done")]);
      setSession({ sessionStatus: "running" });
      expect(screen.queryByTestId("plan-stop")).toBeNull();
      setSession({ sessionStatus: "idle" });
      expect(screen.queryByTestId("plan-continue")).toBeNull();
    } finally {
      act(withdrawContinue);
      act(withdrawStop);
    }
  });

  it("heads the list with the plan's name", () => {
    const named = journalPlan(0, ["baseline"]);
    named.content.name = "Cut GET /orders p95 by 20%";
    showJournal([named]);
    renderPanel();
    expect(screen.getByTestId("tasks-plan-name").textContent).toBe("Cut GET /orders p95 by 20%");
    // The name is the heading, level with "Updated …"; the tab already says Tasks.
    expect(screen.getByRole("heading", { name: "Cut GET /orders p95 by 20%" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Tasks" })).toBeNull();
  });

  it("opens the plan on its graph, with where each task stands and its ⋯ action", async () => {
    const entries = [
      journalPlan(0, ["baseline"], ["batch", ["baseline"]], ["pool", ["baseline"]]),
      status(1, "baseline", "done"),
      status(2, "batch", "failed", "Reverted"),
      status(3, "pool", "needs_input", "Raise the pool size?"),
    ];
    showJournal(entries);
    const question = {
      type: "elicitation",
      elicitationId: "elicit_pool",
      status: "pending",
      phase: "workflow_request_input",
      journalStep: "pool",
    };
    setSession({ blocks: [question] as unknown as ConversationState["blocks"] });
    const taskAction = vi.fn();
    const withdraw = registerTaskAction("conv", taskAction);
    try {
      const { rerender } = renderPanel();
      const showSession = (conversationId: string) =>
        rerender(
          <>
            <TasksPanel conversationId={conversationId} active />
            <SearchProbe />
          </>,
        );
      // The fullscreen glyph a run row's graph button shows too.
      const { container: fullscreenGlyph } = render(<FullscreenIcon />);
      expect(screen.getByTestId("tasks-plan-graph").innerHTML).toBe(fullscreenGlyph.innerHTML);
      fireEvent.click(screen.getByTestId("tasks-plan-graph"));
      const overlay = await screen.findByTestId("workflow-graph-overlay");
      expect(overlay).toHaveAttribute("data-title", "The plan");
      expect(overlay).toHaveAttribute("data-steps", "baseline< batch<baseline pool<baseline");
      expect(overlay).toHaveTextContent("baseline:done batch:failed pool:needs_input");
      fireEvent.click(within(overlay).getByRole("button", { name: "retry batch" }));
      expect(taskAction).toHaveBeenCalledWith("retry", "batch");
      // The open graph follows the journal: once batch starts again, it offers Continue.
      showJournal([...entries, status(4, "batch", "started")]);
      showSession("conv");
      expect(overlay).toHaveTextContent("baseline:done batch:stopped pool:needs_input");
      expect(within(overlay).getByRole("button", { name: "continue batch" })).toBeTruthy();
      // Continue shows the task's question in the chat, which the graph would cover.
      fireEvent.click(within(overlay).getByRole("button", { name: "proceed pool" }));
      expect(screen.queryByTestId("workflow-graph-overlay")).toBeNull();
      const search = new URLSearchParams(screen.getByTestId("search").textContent ?? "");
      expect(search.get("message")).toBe("elicit_pool");

      fireEvent.click(screen.getByTestId("tasks-plan-graph"));
      fireEvent.click(
        within(await screen.findByTestId("workflow-graph-overlay")).getByRole("button", {
          name: "Close graph",
        }),
      );
      expect(screen.queryByTestId("workflow-graph-overlay")).toBeNull();

      // Leaving the session closes the graph, and coming back doesn't reopen it.
      fireEvent.click(screen.getByTestId("tasks-plan-graph"));
      await screen.findByTestId("workflow-graph-overlay");
      showSession("other");
      expect(screen.queryByTestId("workflow-graph-overlay")).toBeNull();
      showSession("conv");
      expect(screen.getByTestId("tasks-plan-graph")).toBeTruthy();
      expect(screen.queryByTestId("workflow-graph-overlay")).toBeNull();
    } finally {
      act(withdraw);
    }
    cleanup();

    // Statuses written without a plan say nothing about what waits on what.
    showJournal([status(1, "baseline", "done")]);
    renderPanel();
    expect(screen.getByTestId("tasks-row")).toBeTruthy();
    expect(screen.queryByTestId("tasks-plan-graph")).toBeNull();
  });

  it("says every task finished when the last one failed", () => {
    showJournal([journalPlan(0, ["a"], ["b"]), status(1, "a", "done"), status(2, "b", "failed")]);
    renderPanel();
    expect(screen.getByTestId("tasks-summary").textContent).toBe("All 2 tasks finished");
    expect(screen.getByText("1 failed")).toBeTruthy();
  });

  it("shows a started task as running only while the session works", () => {
    showJournal([journalPlan(0, ["baseline"]), status(1, "baseline", "started")]);
    renderPanel();
    const step = () => screen.getByTestId("tasks-row");
    expect(step().dataset.state).toBe("stopped");
    expect(step().textContent).toMatch(/Started .+ ago/);

    setSession({ sessionStatus: "running" });
    expect(step().dataset.state).toBe("running");
    expect(within(step()).getByRole("img", { name: "Running" })).toBeTruthy();
  });

  it("counts a running task's time up every second", () => {
    vi.useFakeTimers();
    try {
      const start = status(1, "build", "started");
      vi.setSystemTime((start.created_at + 10) * 1000);
      showJournal([journalPlan(0, ["build"]), start]);
      setSession({ sessionStatus: "running" });
      renderPanel();
      const elapsed = () => within(screen.getByTestId("tasks-row")).getByTitle(/^Started/);
      expect(elapsed().textContent).toBe("10s");
      act(() => vi.advanceTimersByTime(1000));
      expect(elapsed().textContent).toBe("11s");
      act(() => vi.advanceTimersByTime(1000));
      expect(elapsed().textContent).toBe("12s");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps Stop available while background work runs after the main turn ends", () => {
    showJournal([journalPlan(0, ["deploy"]), status(1, "deploy", "started")]);
    const stop = vi.fn();
    const withdraw = registerStopWork("conv", stop);
    try {
      renderPanel();
      const step = () => screen.getByTestId("tasks-row");
      expect(step().dataset.state).toBe("stopped");
      expect(screen.queryByTestId("plan-stop")).toBeNull();
      // The agent's turn ended, but the deploy it left running is still going.
      setSession({ backgroundTaskCount: 1 });
      expect(step().dataset.state).toBe("running");
      fireEvent.click(screen.getByTestId("plan-stop"));
      expect(stop).toHaveBeenCalledOnce();
      setSession({ backgroundTaskCount: 0 });
      expect(step().dataset.state).toBe("stopped");
      expect(screen.queryByTestId("plan-stop")).toBeNull();
    } finally {
      act(withdraw);
    }
  });

  it.each([
    { source: "background work", activity: { background_work_running: true } },
    { source: "child activity", activity: { status: "running" as const } },
  ])("keeps tasks running with an idle parent and sidebar $source", ({ activity }) => {
    showJournal([journalPlan(0, ["deploy"]), status(1, "deploy", "started")]);
    vi.mocked(useOptionalLoadedConversations).mockReturnValue({
      ...conversationPage([conversation("conv", activity)]).data!,
      pageParams: [undefined],
    });
    setSession({ sessionStatus: "running" });
    const { rerender } = renderPanel();

    // An active goal can outlive its turn with no background terminals counted.
    setSession({ sessionStatus: "idle", backgroundTaskCount: 0 });
    expect(screen.getByTestId("tasks-row")).toHaveAttribute("data-state", "running");
    expect(screen.queryByTestId("tasks-paused")).toBeNull();

    // Native task lists use the same activity as journal plans.
    showJournal([]);
    setSession({
      todos: [{ content: "Deploy", status: "in_progress", activeForm: "Deploying" }],
    });
    expect(screen.getByTestId("tasks-row")).toHaveAttribute("data-state", "running");

    let withdrawStop = () => {};
    act(() => {
      withdrawStop = registerStopWork("conv", vi.fn());
    });
    try {
      showJournal([status(1, "deploy", "done")]);
      setSession({
        todos: [{ content: "Deploy", status: "completed", activeForm: "Deploying" }],
      });
      // A status without its plan cannot declare the workflow complete.
      expect(screen.getByTestId("plan-stop")).toBeInTheDocument();
    } finally {
      act(withdrawStop);
    }
    showJournal([]);
    setSession({
      todos: [{ content: "Deploy", status: "in_progress", activeForm: "Deploying" }],
    });

    vi.mocked(useOptionalLoadedConversations).mockReturnValue({
      ...conversationPage([conversation("conv")]).data!,
      pageParams: [undefined],
    });
    rerender(<TasksPanel conversationId="conv" active />);
    expect(screen.getByTestId("tasks-row")).toHaveAttribute("data-state", "stopped");
  });

  it("shows a task waiting for you in pink, and counts it in the summary", () => {
    showJournal([
      journalPlan(0, ["build"], ["approve", ["build"]], ["ship", ["approve"]]),
      status(1, "build", "done", "Image built"),
      status(2, "approve", "needs_input", "Approve the deploy to prod?"),
    ]);
    setSession({ sessionStatus: "running" });
    renderPanel();
    const [, approve, ship] = screen.getAllByTestId("tasks-row");
    expect(approve.dataset.state).toBe("needs_input");
    expect(approve.textContent).toContain("Waiting for you · Approve the deploy to prod?");
    expect(within(approve).getByRole("img", { name: "Waiting for you" })).toBeTruthy();
    expect(within(approve).getByText("Waiting for you")).toHaveClass("text-brand-accent");
    expect(ship.dataset.state).toBe("waiting");
    expect(screen.getByText("1 waiting for you")).toHaveClass("text-brand-accent");
  });

  it.each([
    ["own blocking", {}, true],
    ["own explicit target", { targetSessionId: "conv" }, true],
    ["nonblocking", { isBlocking: false }, false],
    ["mirrored child", { targetSessionId: "child" }, false],
  ] as const)("only pauses for own blocking chat prompts: %s", (_label, fields, waiting) => {
    showJournal([journalPlan(0, ["tests"]), status(1, "tests", "started")]);
    setSession({ sessionStatus: "running" });
    const withdrawStop = registerStopWork("conv", vi.fn());
    const withdrawAction = registerTaskAction("conv", vi.fn());
    try {
      renderPanel();
      const row = () => screen.getByTestId("tasks-row");
      expect(row().dataset.state).toBe("running");
      const prompt = { type: "elicitation", elicitationId: "e1", status: "pending", ...fields };
      setSession({ blocks: [prompt] as unknown as ConversationState["blocks"] });
      expect(row().dataset.state).toBe(waiting ? "needs_input" : "running");
      expect(row().textContent?.includes("Waiting for you · Respond in the chat")).toBe(waiting);
      fireEvent.pointerDown(within(row()).getByTestId("tasks-row-menu"), { button: 0 });
      const action = screen.getByTestId(waiting ? "tasks-row-proceed" : "tasks-row-stop");
      expect(action).toHaveTextContent(waiting ? "Continue" : "Stop");
      fireEvent.click(action);
      setSession({
        blocks: [{ ...prompt, status: "responded" }] as unknown as ConversationState["blocks"],
      });
      expect(row().dataset.state).toBe("running");
    } finally {
      act(withdrawStop);
      act(withdrawAction);
    }
  });

  it("leaves the other running tasks running while a workflow question waits", () => {
    showJournal([
      journalPlan(0, ["tests"], ["docs"]),
      status(1, "tests", "started"),
      status(2, "docs", "needs_input", "Which docs site?"),
    ]);
    const question = {
      type: "elicitation",
      elicitationId: "e1",
      status: "pending",
      phase: "workflow_request_input",
    };
    setSession({
      sessionStatus: "running",
      blocks: [question] as unknown as ConversationState["blocks"],
    });
    renderPanel();
    const [tests, docs] = screen.getAllByTestId("tasks-row");
    expect(tests.dataset.state).toBe("running");
    expect(docs.dataset.state).toBe("needs_input");
    expect(screen.getByText("1 waiting for you")).toBeTruthy();
  });

  it("says an idle list stopped while a task could run, or that an automation's run ended", () => {
    showJournal([journalPlan(0, ["a"], ["b", ["a"]]), status(1, "a", "done", "ok")]);
    const { unmount } = renderPanel();
    expect(screen.getByTestId("tasks-paused").textContent).toBe("· Stopped");
    setSession({ sessionStatus: "running" });
    expect(screen.queryByTestId("tasks-paused")).toBeNull();
    unmount();
    setSession({ sessionStatus: "idle" });
    showJournal([journalPlan(0, ["a"], ["b", ["a"]]), status(1, "a", "done", "ok", RUN)]);
    renderPanel();
    expect(screen.getByTestId("tasks-paused").textContent).toBe("· Run ended");
  });

  it("counts a busy sub-agent as work, but not a side chat", () => {
    showJournal([journalPlan(0, ["deploy"]), status(1, "deploy", "started")]);
    vi.mocked(useOptionalLoadedConversations).mockReturnValue({
      ...conversationPage([conversation("conv", { status: "idle" })]).data!,
      pageParams: [undefined],
    });
    const busy = (labels: Record<string, string>) =>
      vi.mocked(useChildSessions).mockReturnValue({
        children: [{ id: "child", busy: true, labels }] as ChildSessionInfo[],
      } as never);
    const stop = vi.fn();
    const withdraw = registerStopWork("conv", stop);
    try {
      busy({ "omnigent.side_chat": "1", "omnigent.fork.source_id": "conv" });
      const { unmount } = renderPanel();
      expect(screen.getByTestId("tasks-row").dataset.state).toBe("stopped");
      expect(screen.queryByTestId("plan-stop")).toBeNull();
      unmount();
      busy({});
      renderPanel();
      expect(screen.getByTestId("tasks-row").dataset.state).toBe("running");
      fireEvent.click(screen.getByTestId("plan-stop"));
      expect(stop).toHaveBeenCalledOnce();
    } finally {
      act(withdraw);
    }
  });

  it("marks where a task restarted in its history", () => {
    showJournal([
      journalPlan(0, ["deploy"]),
      status(1, "deploy", "started"),
      status(2, "deploy", "failed", "Stopped during the build"),
      status(3, "deploy", "started", "Redeploying"),
    ]);
    renderPanel();
    fireEvent.click(within(screen.getByTestId("tasks-row")).getByTestId("tasks-row-history"));
    const history = screen.getByRole("list", { name: "deploy history" });
    expect(
      within(history)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      expect.stringMatching(/^Started/),
      expect.stringMatching(/^Failed · Stopped during the build/),
      "Restarted",
      expect.stringMatching(/^Started · Redeploying/),
    ]);
  });

  it("opens a task's history: what it waits on, then its entries, oldest first", () => {
    showJournal([
      journalPlan(0, ["profile"], ["batch"], ["pool", ["profile", "batch"]]),
      status(1, "pool", "started"),
      status(2, "pool", "failed", "Reverted"),
    ]);
    renderPanel();
    const pool = screen.getAllByTestId("tasks-row")[2];
    fireEvent.click(within(pool).getByTestId("tasks-row-history"));
    expect(within(pool).getByText(/^After/).textContent).toBe("After profile and batch");
    const history = screen.getByRole("list", { name: "pool history" });
    expect(
      within(history)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([expect.stringMatching(/^Started/), expect.stringMatching(/^Failed · Reverted/)]);
  });

  it("shows when a finished task ended, stamped the way chat messages are", async () => {
    const done = status(2, "build", "done");
    const failed = status(4, "deploy", "failed", "Reverted");
    const doneWithoutStart = status(5, "docs", "done");
    showJournal([
      journalPlan(0, ["build"], ["deploy", ["build"]], ["docs"]),
      status(1, "build", "started"),
      done,
      status(3, "deploy", "started"),
      failed,
      doneWithoutStart,
    ]);
    renderPanel();
    // ICU may put a narrow no-break space before AM/PM; queries collapse it.
    const ended = (entry: JournalEntry) =>
      formatMessageTimestamp(entry.created_at).replace(/\s/g, " ");
    const [build, deploy, docs] = screen.getAllByTestId("tasks-row");
    // The end time replaces how long the task ran, a minute here.
    expect(within(build).getByText(ended(done)).tagName).toBe("TIME");
    expect(within(build).queryByText("1m")).toBeNull();
    expect(within(deploy).getByText(ended(failed))).toBeTruthy();
    expect(within(docs).getByText(ended(doneWithoutStart))).toBeTruthy();
    fireEvent.focus(screen.getAllByTestId("tasks-progress-segment")[0]);
    expect(await screen.findByTestId("tasks-progress-card")).toHaveTextContent(
      `Done · ${ended(done)}`,
    );
  });

  it("leaves out the agent's notes, under a task, in its history and on their own", () => {
    showJournal([
      journalPlan(0, ["pool"]),
      status(1, "pool", "started"),
      journalEntry(2, { step: "pool", content: { body: "Cold connects are the slowest" } }),
      journalEntry(3, { content: { body: "Measure first" } }),
    ]);
    renderPanel();
    fireEvent.click(within(screen.getByTestId("tasks-row")).getByTestId("tasks-row-history"));
    const history = screen.getByRole("list", { name: "pool history" });
    expect(
      within(history)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([expect.stringMatching(/^Started/)]);
    expect(screen.queryByText(/Cold connects|Measure first/)).toBeNull();
    expect(screen.queryByRole("region", { name: "Notes" })).toBeNull();
  });

  it("has no tasks yet when the agent wrote only notes, and no Runs section", () => {
    showJournal([
      journalEntry(0, { content: { body: "Measure first" } }),
      journalEntry(1, { scheduled_task_run_id: RUN, content: { body: "Stopped at the try cap" } }),
    ]);
    renderPanel();
    expect(screen.getByText("No tasks yet")).toBeTruthy();
    expect(screen.queryByText(/Measure first|try cap/)).toBeNull();
    expect(screen.queryByRole("region", { name: "Runs" })).toBeNull();
  });

  it("offers no older entries, and names no workers", () => {
    // Busy sub-agents show in the Agents tab; the Tasks tab lists only tasks.
    vi.mocked(useChildSessions).mockReturnValue({
      children: [{ id: "c1", busy: true, task_summary: "fix-3", title: "codex:fix-3" }],
    } as never);
    showJournal([journalPlan(0, ["a"])], { hasOlder: true });
    renderPanel();
    expect(screen.queryByText(/workers? running/)).toBeNull();
    expect(screen.queryByRole("button", { name: /older/ })).toBeNull();
  });
});
