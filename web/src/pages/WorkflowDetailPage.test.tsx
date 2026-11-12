import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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
import type * as RoutingModule from "@/lib/routing";
import type { JournalEntry, StepProgress } from "@/lib/sessionJournal";
import { openTasksTab } from "@/store/sessionTasksActions";
import type * as TasksActions from "@/store/sessionTasksActions";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";
import type { WorkflowRun } from "@/lib/workflowRuns";

import { WorkflowDetailPage } from "./WorkflowDetailPage";

vi.mock("@/hooks/useWorkflow", async (importOriginal) => {
  const actual = await importOriginal<typeof WorkflowHooks>();
  return { ...actual, useWorkflow: vi.fn(), useWorkflowRuns: vi.fn() };
});
vi.mock("@/hooks/useSavedWorkflows", () => ({
  useDeleteWorkflow: vi.fn(),
  useSavedWorkflows: vi.fn(),
}));
vi.mock("@/store/sessionTasksActions", async (importOriginal) => {
  const actual = await importOriginal<typeof TasksActions>();
  return { ...actual, openTasksTab: vi.fn() };
});
vi.mock("@/hooks/useNow", () => ({ useNow: () => new Date("2026-11-12T18:00:00Z") }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => "loading" }));
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

function renderPage() {
  return render(
    <MemoryRouter>
      <WorkflowDetailPage />
    </MemoryRouter>,
  );
}

describe("WorkflowDetailPage", () => {
  beforeEach(() => {
    navigate.mockClear();
    deleteWorkflow.mockClear();
    vi.mocked(openTasksTab).mockClear();
    vi.mocked(showToast).mockClear();
    vi.mocked(useDeleteWorkflow).mockReturnValue({
      mutate: deleteWorkflow,
      isPending: false,
    } as unknown as ReturnType<typeof useDeleteWorkflow>);
  });
  afterEach(cleanup);

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

  it("runs it, makes an automation of it, or deletes it and goes back", () => {
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
    const [id, { onSuccess, onError }] = deleteWorkflow.mock.calls[0] as [
      string,
      { onSuccess: () => void; onError: () => void },
    ];
    expect(id).toBe(WORKFLOW.id);
    onSuccess();
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
