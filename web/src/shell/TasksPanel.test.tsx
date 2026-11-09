import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type * as UseSessionJournalModule from "@/hooks/useSessionJournal";
import type * as CapabilitiesModule from "@/lib/CapabilitiesContext";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type ChildSessionInfo, useChildSessions } from "@/hooks/useChildSessions";
import { type SessionJournalView, useSessionJournal } from "@/hooks/useSessionJournal";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { JournalEntry } from "@/lib/sessionJournal";
import type { ConversationState } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import {
  openTasksTab,
  registerStartWorkflow,
  useSessionTasksActions,
} from "@/store/sessionTasksActions";
import { journalEntry, journalPlan } from "@/test/journalFixtures";
import { TasksPanel } from "./TasksPanel";

vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: vi.fn(),
}));
vi.mock("@/hooks/useSessionJournal", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionJournalModule>()),
  useSessionJournal: vi.fn(),
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
    payload: { status: value, text: text ?? null },
  });
}

/** Shows the URL's search string, where a task's click asks the chat for its marker. */
function SearchProbe() {
  return <output data-testid="search">{useLocation().search}</output>;
}

function renderPanel(onStartWorkflow?: () => void, onShowInChat?: () => void) {
  return render(
    <MemoryRouter initialEntries={["/c/conv"]}>
      <TasksPanel
        conversationId="conv"
        active
        onStartWorkflow={onStartWorkflow}
        onShowInChat={onShowInChat}
      />
      <SearchProbe />
    </MemoryRouter>,
  );
}

/** The session's live state: whether it works, and the agent's own task list. */
function setSession(state: Partial<ConversationState>) {
  act(() => conversationRegistry.acquire("conv").setState(state));
}

beforeEach(() => {
  vi.mocked(useChildSessions).mockReturnValue({ children: [] } as never);
  vi.mocked(useServerInfo).mockReturnValue({ session_journal: true } as never);
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

  it("shows the agent's own list on a server that keeps no journal", () => {
    vi.mocked(useServerInfo).mockReturnValue({ session_journal: false } as never);
    showJournal([journalPlan(0, ["profile"])]);
    setSession({ todos: [{ content: "Only item", status: "pending", activeForm: "" }] });
    renderPanel();
    expect(screen.getByTestId("tasks-row").textContent).toContain("Only item");
    expect(vi.mocked(useSessionJournal)).toHaveBeenLastCalledWith("conv", false);
  });

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
      status(6, "tests", "blocked", "The test database is down"),
    ]);
    renderPanel();
    const steps = screen.getAllByTestId("tasks-row");
    expect(steps.map((step) => step.dataset.state)).toEqual([
      "done",
      "done",
      "failed",
      "blocked",
      "waiting",
    ]);
    expect(steps[0].textContent).toContain("p95 434 ms");
    expect(within(steps[3]).getByText("Blocked")).toBeTruthy();
    expect(within(steps[4]).getByRole("img", { name: "Not started" }).textContent).toBe("5");
    // The failed try counts as finished; the count beside the summary names it.
    expect(screen.getByTestId("tasks-summary").textContent).toBe("3 of 5 finished");
    expect(screen.getByText("1 failed")).toBeTruthy();
  });

  it("marks every step that can start as ready", () => {
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
  });

  it("says on hover which task a progress segment is and where it stands", async () => {
    showJournal([
      journalPlan(0, ["profile"], ["cache", ["profile"]], ["ship", ["cache"]]),
      status(1, "profile", "done"),
    ]);
    renderPanel();
    const segments = screen.getAllByTestId("tasks-progress-segment");
    fireEvent.focus(segments[1]);
    const ready = await screen.findByRole("tooltip");
    expect(ready).toHaveTextContent("Task 2 of 3");
    expect(ready).toHaveTextContent("cache");
    expect(ready).toHaveTextContent("Ready to start");
    fireEvent.blur(segments[1]);
    fireEvent.focus(segments[2]);
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("Waiting on cache"));
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

  it("takes a started task's click to its newest marker in the chat; one not started doesn't link", () => {
    const onShowInChat = vi.fn();
    showJournal([
      journalPlan(0, ["deploy"], ["verify", ["deploy"]], ["video", ["verify"]]),
      status(1, "deploy", "started"),
      status(2, "deploy", "done", "Pod 1/1"),
      status(3, "verify", "started"),
    ]);
    renderPanel(undefined, onShowInChat);
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
    }
    expect(onShowInChat).toHaveBeenCalledTimes(2);
    expect(within(video).queryByTestId("tasks-row-show-in-chat")).toBeNull();
    // Its history opens from the chevron instead.
    expect(within(video).getByTestId("tasks-row-history")).toBeTruthy();
  });

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

  it("heads the list with the plan's name", () => {
    const named = journalPlan(0, ["baseline"]);
    named.payload.name = "Cut GET /orders p95 by 20%";
    showJournal([named]);
    renderPanel();
    expect(screen.getByTestId("tasks-plan-name").textContent).toBe("Cut GET /orders p95 by 20%");
    // The name is the heading, level with "Updated …"; the tab already says Tasks.
    expect(screen.getByRole("heading", { name: "Cut GET /orders p95 by 20%" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Tasks" })).toBeNull();
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

  it("keeps a started task running while a background task it started runs on", () => {
    showJournal([journalPlan(0, ["deploy"]), status(1, "deploy", "started")]);
    renderPanel();
    const step = () => screen.getByTestId("tasks-row");
    expect(step().dataset.state).toBe("stopped");
    // The agent's turn ended, but the deploy it left running is still going.
    setSession({ backgroundTaskCount: 1 });
    expect(step().dataset.state).toBe("running");
    setSession({ backgroundTaskCount: 0 });
    expect(step().dataset.state).toBe("stopped");
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

  it("shows a running task waiting for you while a prompt in the chat waits", () => {
    showJournal([journalPlan(0, ["tests"]), status(1, "tests", "started")]);
    setSession({ sessionStatus: "running" });
    renderPanel();
    const row = () => screen.getByTestId("tasks-row");
    expect(row().dataset.state).toBe("running");
    const prompt = { type: "elicitation", elicitationId: "e1", status: "pending" };
    setSession({ blocks: [prompt] as unknown as ConversationState["blocks"] });
    expect(row().dataset.state).toBe("needs_input");
    expect(row().textContent).toContain("Waiting for you · Respond in the chat");
    setSession({
      blocks: [{ ...prompt, status: "responded" }] as unknown as ConversationState["blocks"],
    });
    expect(row().dataset.state).toBe("running");
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
    const busy = (labels: Record<string, string>) =>
      vi.mocked(useChildSessions).mockReturnValue({
        children: [{ id: "child", busy: true, labels }] as ChildSessionInfo[],
      } as never);
    busy({ "omnigent.side_chat": "1", "omnigent.fork.source_id": "conv" });
    const { unmount } = renderPanel();
    expect(screen.getByTestId("tasks-row").dataset.state).toBe("stopped");
    unmount();
    busy({});
    renderPanel();
    expect(screen.getByTestId("tasks-row").dataset.state).toBe("running");
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

  it("shows a task's newest note under it, shortened until opened", () => {
    const body = `## The pool didn't move p95\n${"Cold connects are the slowest requests. ".repeat(6)}**Next**: pre-warm it.`;
    showJournal([journalPlan(0, ["pool"]), journalEntry(1, { step: "pool", payload: { body } })]);
    renderPanel();
    const note = screen.getByTestId("tasks-note");
    expect(within(note).getByText("The pool didn't move p95")).toBeTruthy();
    expect(within(note).getByText("Next").tagName).toBe("STRONG");
    const more = within(note).getByRole("button", { name: "Show more" });
    fireEvent.click(more);
    expect(more.textContent).toBe("Show less");
  });

  it("lists notes that aren't about one task, newest first, with no Runs section", () => {
    showJournal([
      journalEntry(0, { payload: { body: "Measure first" } }),
      journalEntry(1, { scheduled_task_run_id: RUN, payload: { body: "Stopped at the try cap" } }),
    ]);
    renderPanel();
    const notes = screen.getByRole("region", { name: "Notes" });
    expect(
      within(notes)
        .getAllByTestId("tasks-note")
        .map((note) => note.textContent),
    ).toEqual(["Stopped at the try cap", "Measure first"]);
    expect(screen.queryByRole("region", { name: "Runs" })).toBeNull();
  });

  it("loads older entries on request, and names no workers", () => {
    // Busy sub-agents show in the Agents tab; the Tasks tab lists only tasks.
    vi.mocked(useChildSessions).mockReturnValue({
      children: [{ id: "c1", busy: true, task_summary: "fix-3", title: "codex:fix-3" }],
    } as never);
    const view = showJournal([journalEntry(5)], { hasOlder: true });
    renderPanel();
    expect(screen.queryByText(/workers? running/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show older notes" }));
    expect(view.loadOlder).toHaveBeenCalled();
  });
});
