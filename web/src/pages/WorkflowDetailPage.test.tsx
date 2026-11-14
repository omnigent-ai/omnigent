import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import type { WorkflowSessionRequest } from "@/components/workflows/WorkflowSessionDialog";
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { useDeleteWorkflow } from "@/hooks/useSavedWorkflows";
import {
  useWorkflow,
  useWorkflowRuns,
  type WorkflowDetail,
  WorkflowNotFoundError,
} from "@/hooks/useWorkflow";
import type * as WorkflowHooks from "@/hooks/useWorkflow";
import { useSession } from "@/hooks/useSession";
import { useSessionJournal } from "@/hooks/useSessionJournal";
import type * as SessionJournalHooks from "@/hooks/useSessionJournal";
import { authenticatedFetch } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type * as IdentityModule from "@/lib/identity";
import type * as RoutingModule from "@/lib/routing";
import { sessionJournalKey, type JournalEntry, type StepProgress } from "@/lib/sessionJournal";
import { sendUserMessage } from "@/lib/sessionsApi";
import type * as SessionsApi from "@/lib/sessionsApi";
import { openTasksTab } from "@/store/sessionTasksActions";
import type * as TasksActions from "@/store/sessionTasksActions";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";
import type { WorkflowRun } from "@/lib/workflowRuns";
import type { Session } from "@/lib/types";
import { journalPage } from "@/test/journalFixtures";

import { WorkflowDetailPage } from "./WorkflowDetailPage";

vi.mock("@/hooks/useWorkflow", async (importOriginal) => {
  const actual = await importOriginal<typeof WorkflowHooks>();
  return { ...actual, useWorkflow: vi.fn(), useWorkflowRuns: vi.fn() };
});
vi.mock("@/hooks/useSessionJournal", () => ({ useSessionJournal: vi.fn() }));
vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
// The real chat has its own stream/composer tests; this page owns its placement and session link.
vi.mock("@/components/workflows/WorkflowPlanningChat", () => ({
  WorkflowPlanningChat: ({
    workflow,
    onClose,
  }: {
    workflow: WorkflowDetail;
    onClose: () => void;
  }) => (
    <div data-testid="planning-chat" data-session={workflow.planning_session_id}>
      <button type="button" onClick={onClose}>
        Close planning chat
      </button>
    </div>
  ),
}));
vi.mock("@/hooks/useSavedWorkflows", () => ({
  useDeleteWorkflow: vi.fn(),
  useSavedWorkflows: vi.fn(),
}));
vi.mock("@/store/sessionTasksActions", async (importOriginal) => {
  const actual = await importOriginal<typeof TasksActions>();
  return { ...actual, openTasksTab: vi.fn() };
});
vi.mock("@/lib/sessionsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof SessionsApi>();
  return { ...actual, interrupt: vi.fn(), sendUserMessage: vi.fn() };
});
vi.mock("@/hooks/useNow", () => ({ useNow: () => new Date("2026-11-12T18:00:00Z") }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn(() => "loading") }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
// The graph has its own tests; here it lists the steps it was given, and the
// full-screen view where each stands.
vi.mock("@/components/workflows/WorkflowStepsGraph", () => ({
  WorkflowStepsGraph: ({ steps }: { steps: WorkflowStep[] }) => (
    <div data-testid="workflow-steps-graph">{steps.map((step) => step.name).join(", ")}</div>
  ),
  WorkflowGraphOverlay: (props: {
    title: string;
    progress?: StepProgress[];
    onClose: () => void;
  }) => (
    <div data-testid="workflow-graph-overlay" data-title={props.title}>
      {props.progress?.map((item) => `${item.step.id}:${item.state}`).join(" ")}
      <button type="button" onClick={props.onClose}>
        Close graph
      </button>
    </div>
  ),
}));
vi.mock("@/components/workflows/WorkflowSessionDialog", () => ({
  WorkflowSessionDialog: (props: { request: WorkflowSessionRequest | null; open: boolean }) =>
    props.open ? (
      <div data-testid="workflow-session-dialog">{JSON.stringify(props.request)}</div>
    ) : null,
}));
vi.mock("@/components/scheduled/CreateScheduledTaskDialog", () => ({
  CreateScheduledTaskDialog: (props: { open: boolean; lockedWorkflow?: SavedWorkflow | null }) =>
    props.open ? <div data-testid="new-automation-dialog">{props.lockedWorkflow?.name}</div> : null,
}));
const navigate = vi.fn();
vi.mock("@/lib/routing", async (importOriginal) => {
  const actual = await importOriginal<typeof RoutingModule>();
  return {
    ...actual,
    useNavigate: () => navigate,
    useParams: () => ({ workflowId: WORKFLOW.id }),
  };
});

const WORKFLOW: WorkflowDetail = {
  id: "6f1c2b9a4d8e4f30a1b2c3d4e5f60718",
  name: "Triage flaky tests",
  description: "Finds a flaky test and fixes it.\nGive it: the test suite.",
  steps: [
    { id: "explore", name: "Explore the failures", goal: null, depends_on: [] },
    { id: "fix", name: "Fix the test", goal: "The suite passes", depends_on: ["explore"] },
  ],
};

function status(step: string, value: string): JournalEntry {
  return {
    id: `${step}-${value}`,
    position: 1,
    created_at: Date.parse("2026-11-12T15:05:00Z") / 1000,
    scheduled_task_run_id: null,
    step,
    author: { type: "agent" },
    kind: "status",
    payload: { status: value, text: "x" },
  };
}

function run(
  sessionId: string,
  title: string,
  progress: Partial<WorkflowRun["progress"]>,
  statuses: JournalEntry[] = [],
): WorkflowRun {
  return {
    id: `plan-${sessionId}`,
    sessionId,
    title,
    startedAt: Date.parse("2026-11-12T15:04:00Z") / 1000,
    sessionStatus: "idle" as const,
    archived: false,
    progress: { total: 2, started: 0, done: 0, needsInput: 0, failed: 0, ...progress },
    steps: WORKFLOW.steps,
    statuses,
  };
}

function mockWorkflow(state: Partial<ReturnType<typeof useWorkflow>>) {
  const refetch = vi.fn();
  vi.mocked(useWorkflow).mockReturnValue({
    data: WORKFLOW,
    isLoading: false,
    isError: false,
    error: null,
    refetch,
    ...state,
  } as unknown as ReturnType<typeof useWorkflow>);
  return refetch;
}

function mockRuns(runs: WorkflowRun[], state: Record<string, unknown> = {}) {
  const fetchNextPage = vi.fn();
  vi.mocked(useWorkflowRuns).mockReturnValue({
    data: { pages: [{ runs, nextCursor: null }], pageParams: [null] },
    isLoading: false,
    isError: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage,
    ...state,
  } as unknown as ReturnType<typeof useWorkflowRuns>);
  return fetchNextPage;
}

const deleteWorkflow = vi.fn();

function renderPage(client = new QueryClient()) {
  // A run's play and stop buttons refresh the runs through the query client.
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WorkflowDetailPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("WorkflowDetailPage", () => {
  beforeEach(() => {
    vi.mocked(useServerInfo).mockReturnValue("loading");
    navigate.mockClear();
    deleteWorkflow.mockClear();
    vi.mocked(openTasksTab).mockClear();
    vi.mocked(showToast).mockClear();
    vi.mocked(useSession).mockImplementation((id) => ({
      session: id ? ({ id, archived: false, labels: {} } as Session) : null,
      isLoading: false,
      error: null,
    }));
    vi.mocked(useSessionJournal).mockReturnValue({
      entries: [],
      isLoading: false,
      error: null,
      hasOlder: false,
      loadingOlder: false,
      loadOlder: vi.fn(),
    });
    vi.mocked(useDeleteWorkflow).mockReturnValue({
      mutate: deleteWorkflow,
      isPending: false,
    } as unknown as ReturnType<typeof useDeleteWorkflow>);
  });
  afterEach(cleanup);

  it("keeps a draft unrunnable until its proposed plan saves, then reopens the same chat", async () => {
    const actual = await vi.importActual<typeof WorkflowHooks>("@/hooks/useWorkflow");
    vi.mocked(useWorkflow).mockImplementation(actual.useWorkflow);
    mockRuns([]);
    const planner = "a".repeat(32);
    let stored: WorkflowDetail = {
      ...WORKFLOW,
      steps: [],
      is_draft: true,
      planning_session_id: planner,
    };
    let rejectSave = true;
    vi.mocked(authenticatedFetch).mockImplementation(async (_path, options) => {
      if (options?.method === "PATCH") {
        if (rejectSave)
          return new Response(
            JSON.stringify({ error: { message: "Try again", type: "server_error" } }),
            { status: 500 },
          );
        const body = JSON.parse(String(options.body));
        stored = { ...stored, steps: body.steps, is_draft: false };
      }
      return new Response(JSON.stringify(stored));
    });
    const proposal: JournalEntry = {
      id: "proposal",
      position: 1,
      created_at: 1,
      scheduled_task_run_id: null,
      step: null,
      author: { type: "agent" },
      kind: "plan",
      payload: { steps: WORKFLOW.steps },
    };
    vi.mocked(useSessionJournal).mockReturnValue({
      entries: [proposal],
      isLoading: false,
      error: null,
      hasOlder: false,
      loadingOlder: false,
      loadOlder: vi.fn(),
    });
    renderPage();

    expect(await screen.findByText("Draft")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-steps-graph")).toHaveTextContent(
      "Explore the failures, Fix the test",
    );
    expect(screen.getByTestId("planning-chat")).toHaveAttribute("data-session", planner);
    expect(screen.getByTestId("workflow-detail-run")).toBeDisabled();
    expect(screen.getByTestId("workflow-detail-create-automation")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t save the plan");
    expect(screen.getByTestId("workflow-detail-run")).toBeDisabled();

    rejectSave = false;
    fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
    await waitFor(() => expect(screen.getByTestId("workflow-detail-run")).toBeEnabled());
    expect(stored.steps).toEqual(WORKFLOW.steps);
    expect(screen.getByTestId("planning-chat")).toBeInTheDocument();
    expect(screen.queryByText("Draft")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Saved plan");

    cleanup();
    renderPage();
    await screen.findByTestId("workflow-detail-title");
    fireEvent.click(screen.getByRole("button", { name: "Edit workflow" }));
    expect(screen.getByTestId("planning-chat")).toHaveAttribute("data-session", planner);
    expect(screen.getByTestId("workflow-detail-run")).toBeEnabled();
  });

  it("shows the workflow, its steps, and the sessions that ran it", () => {
    mockWorkflow({});
    mockRuns([
      run("a".repeat(32), "Fix the upload test", { needsInput: 1 }),
      run("b".repeat(32), "", { done: 2 }),
    ]);
    renderPage();

    expect(screen.getByTestId("workflow-detail-title")).toHaveTextContent("Triage flaky tests");
    expect(screen.getByTestId("workflow-detail-description").textContent).toBe(
      WORKFLOW.description,
    );
    // It fits in three lines, so there's nothing to expand.
    expect(screen.queryByTestId("workflow-detail-description-toggle")).toBeNull();
    expect(screen.getByTestId("workflow-steps-graph")).toHaveTextContent(
      "Explore the failures, Fix the test",
    );
    const [waiting, done] = screen.getAllByTestId("workflow-run");
    expect(within(waiting!).getByRole("link")).toHaveAttribute("href", `/c/${"a".repeat(32)}`);
    expect(within(waiting!).getByText("Fix the upload test")).toBeInTheDocument();
    expect(within(waiting!).getByTestId("workflow-run-state")).toHaveTextContent("Waiting for you");
    expect(within(done!).getByText("Untitled session")).toBeInTheDocument();
    expect(done).toHaveAttribute("data-run-state", "done");
  });

  it.each(["deleted", "archived"] as const)(
    "keeps the saved plan without polling a %s planning session while setup is open",
    async (state) => {
      const journalHooks = await vi.importActual<typeof SessionJournalHooks>(
        "@/hooks/useSessionJournal",
      );
      vi.mocked(useSessionJournal).mockImplementation(journalHooks.useSessionJournal);
      const planner = "d".repeat(32);
      mockWorkflow({ data: { ...WORKFLOW, planning_session_id: planner } });
      mockRuns([]);
      vi.mocked(useSession).mockReturnValue({
        session: state === "archived" ? ({ id: planner, archived: true } as Session) : null,
        isLoading: false,
        error: state === "deleted" ? new Error("Session not found") : null,
      });
      vi.mocked(authenticatedFetch).mockReset();
      vi.mocked(authenticatedFetch).mockImplementation(
        async () => new Response(null, { status: 404 }),
      );
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      client.setQueryData(sessionJournalKey(planner), {
        pages: [journalPage([])],
        pageParams: [null],
      });

      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        renderPage(client);
        fireEvent.click(screen.getByRole("button", { name: "Edit workflow" }));
        await act(async () =>
          vi.advanceTimersByTimeAsync(journalHooks.JOURNAL_FALLBACK_REFETCH_MS * 2),
        );

        expect(screen.getByTestId("planning-chat")).toBeInTheDocument();
        expect(screen.getByTestId("workflow-steps-graph")).toHaveTextContent(
          "Explore the failures, Fix the test",
        );
        expect(screen.getByTestId("workflow-detail-run")).toBeEnabled();
        expect(screen.queryByRole("alert")).toBeNull();
        expect(authenticatedFetch).not.toHaveBeenCalled();
      } finally {
        cleanup();
        client.clear();
        vi.useRealTimers();
      }
    },
  );

  it("shows three lines of a long description, and the rest on request", () => {
    // jsdom lays nothing out: report a paragraph taller than its three lines.
    const scroll = vi.spyOn(Element.prototype, "scrollHeight", "get").mockReturnValue(120);
    const client = vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(60);
    try {
      mockWorkflow({});
      mockRuns([]);
      renderPage();
      const description = screen.getByTestId("workflow-detail-description");
      expect(description).toHaveClass("line-clamp-3");

      fireEvent.click(screen.getByRole("button", { name: "Description" }));
      expect(description).not.toHaveClass("line-clamp-3");
      expect(screen.getByRole("button", { name: "Description" })).toHaveAttribute(
        "aria-expanded",
        "true",
      );
    } finally {
      scroll.mockRestore();
      client.mockRestore();
    }
  });

  it("shows each run's tasks as the Tasks tab's timeline, and opens a task in its session", () => {
    mockWorkflow({});
    const session = "a".repeat(32);
    mockRuns([run(session, "Fix the upload test", { done: 1 }, [status("explore", "done")])]);
    renderPage();

    const row = screen.getByTestId("workflow-run");
    const segments = within(row).getAllByTestId("tasks-progress-segment");
    expect(segments.map((segment) => segment.getAttribute("aria-label"))).toEqual([
      "Task 1, Explore the failures: Done",
      "Task 2, Fix the test: Ready",
    ]);
    fireEvent.click(segments[1]!);
    expect(openTasksTab).toHaveBeenCalledWith(session, "fix");
    expect(navigate).toHaveBeenCalledWith(`/c/${session}`);
  });

  it("offers play on a run that stopped with tasks left, and stop on a running one", () => {
    mockWorkflow({});
    mockRuns([
      run("a".repeat(32), "Stopped run", { done: 1 }, [status("explore", "done")]),
      {
        ...run("b".repeat(32), "Running run", { started: 1 }, [status("explore", "started")]),
        sessionStatus: "running",
      },
      run("c".repeat(32), "Finished run", { done: 2 }, [
        status("explore", "done"),
        status("fix", "done"),
      ]),
    ]);
    renderPage();

    const [stopped, running, finished] = screen.getAllByTestId("workflow-run");
    expect(within(stopped!).getByRole("button", { name: "Continue the plan" })).toBeInTheDocument();
    expect(within(running!).getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(within(finished!).queryByTestId("plan-continue")).toBeNull();
    expect(within(finished!).queryByTestId("plan-stop")).toBeNull();
  });

  it("sends a task's ⋯ action on a run's timeline card to the run's session", async () => {
    vi.mocked(sendUserMessage).mockResolvedValue({ queued: true });
    mockWorkflow({});
    const session = "a".repeat(32);
    mockRuns([
      run(session, "Fix the upload test", { done: 1, failed: 1 }, [
        status("explore", "done"),
        status("fix", "failed"),
      ]),
      {
        ...run("b".repeat(32), "Archived run", { failed: 1 }, [status("explore", "failed")]),
        archived: true,
      },
    ]);
    renderPage();

    const [live, archived] = screen.getAllByTestId("workflow-run");
    fireEvent.focus(within(live!).getAllByTestId("tasks-progress-segment")[1]!);
    fireEvent.pointerDown(await screen.findByTestId("tasks-progress-menu"), { button: 0 });
    fireEvent.click(screen.getByTestId("tasks-progress-retry"));
    await waitFor(() =>
      expect(sendUserMessage).toHaveBeenCalledWith(
        session,
        'Retry the task "Fix the test", then go on with the rest of the plan.',
      ),
    );
    await waitFor(() => expect(screen.queryByTestId("tasks-progress-card")).toBeNull());

    // An archived run's cards offer nothing.
    fireEvent.focus(within(archived!).getAllByTestId("tasks-progress-segment")[0]!);
    expect(await screen.findByTestId("tasks-progress-card")).toHaveTextContent(
      "Explore the failures",
    );
    expect(screen.queryByTestId("tasks-progress-menu")).toBeNull();
  });

  it("shows a run on the workflow's graph, where each task stands", () => {
    mockWorkflow({});
    mockRuns([
      run("a".repeat(32), "Fix the upload test", { needsInput: 1 }, [
        status("explore", "done"),
        status("fix", "needs_input"),
      ]),
    ]);
    renderPage();
    expect(screen.queryByTestId("workflow-graph-overlay")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "View Fix the upload test on the graph" }));
    const overlay = screen.getByTestId("workflow-graph-overlay");
    expect(overlay).toHaveAttribute("data-title", "Triage flaky tests");
    expect(overlay).toHaveTextContent("explore:done fix:needs_input");
    fireEvent.click(within(overlay).getByRole("button", { name: "Close graph" }));
    expect(screen.queryByTestId("workflow-graph-overlay")).toBeNull();
  });

  it("says when nothing ran it yet, and loads older runs on request", () => {
    mockWorkflow({});
    mockRuns([]);
    const { unmount } = renderPage();
    expect(screen.getByTestId("workflow-runs-empty")).toHaveTextContent("No runs yet.");
    unmount();

    const fetchNextPage = mockRuns([run("a".repeat(32), "One", {})], { hasNextPage: true });
    renderPage();
    fireEvent.click(screen.getByTestId("workflow-runs-more"));
    expect(fetchNextPage).toHaveBeenCalled();
  });

  it("runs it, makes an automation of it, or confirms deletion and goes back", () => {
    vi.mocked(useServerInfo).mockReturnValue({
      ...FALLBACK_SERVER_INFO,
      automations_enabled: false,
    });
    mockWorkflow({});
    mockRuns([]);
    renderPage();

    fireEvent.click(screen.getByTestId("workflow-detail-run"));
    expect(JSON.parse(screen.getByTestId("workflow-session-dialog").textContent!)).toEqual({
      kind: "run",
      workflow: { id: WORKFLOW.id, name: WORKFLOW.name, description: WORKFLOW.description },
    });
    fireEvent.click(screen.getByTestId("workflow-detail-create-automation"));
    expect(screen.getByTestId("new-automation-dialog")).toHaveTextContent("Triage flaky tests");

    fireEvent.click(screen.getByRole("button", { name: "Delete workflow" }));
    expect(deleteWorkflow).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: "Delete workflow?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete workflow" }));
    const [id, { onSuccess, onError }] = deleteWorkflow.mock.calls[0] as [
      string,
      { onSuccess: () => void; onError: () => void },
    ];
    expect(id).toBe(WORKFLOW.id);
    act(onSuccess);
    expect(navigate).toHaveBeenCalledWith("/workflows");
    onError();
    expect(showToast).toHaveBeenCalledWith("Couldn’t delete “Triage flaky tests”.");
  });

  it("tells a missing workflow from one that failed to load", () => {
    mockWorkflow({ data: undefined, isError: true, error: new WorkflowNotFoundError() });
    mockRuns([]);
    const { unmount } = renderPage();
    expect(screen.getByTestId("workflow-detail-not-found")).toBeInTheDocument();
    unmount();

    const refetch = mockWorkflow({ data: undefined, isError: true, error: new Error("500") });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(refetch).toHaveBeenCalled();
  });
});
