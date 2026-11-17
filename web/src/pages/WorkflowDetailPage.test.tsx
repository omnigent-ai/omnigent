import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import { SessionDescendantRuns } from "@/components/workflows/WorkflowRunRow";
import type { WorkflowSessionRequest } from "@/components/workflows/WorkflowSessionDialog";
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { useDeleteWorkflow, useSavedWorkflows } from "@/hooks/useSavedWorkflows";
import {
  useWorkflow,
  useWorkflowRunDescendants,
  useWorkflowRuns,
  type WorkflowDetail,
  WorkflowNotFoundError,
} from "@/hooks/useWorkflow";
import type * as WorkflowHooks from "@/hooks/useWorkflow";
import { useSession } from "@/hooks/useSession";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useSessionJournal } from "@/hooks/useSessionJournal";
import type * as SessionJournalHooks from "@/hooks/useSessionJournal";
import { useOptionalLoadedConversations } from "@/hooks/useSidebarData";
import type * as SidebarDataModule from "@/hooks/useSidebarData";
import { authenticatedFetch } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type * as IdentityModule from "@/lib/identity";
import type * as RoutingModule from "@/lib/routing";
import { sessionJournalKey, type JournalEntry, type StepProgress } from "@/lib/sessionJournal";
import { ApiError, getSession, sendUserMessage } from "@/lib/sessionsApi";
import { taskActionMessage } from "@/lib/workflow";
import type * as SessionsApi from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { openTasksTab } from "@/store/sessionTasksActions";
import type * as TasksActions from "@/store/sessionTasksActions";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";
import type { WorkflowRun } from "@/lib/workflowRuns";
import type { Session } from "@/lib/types";
import { journalPage } from "@/test/journalFixtures";
import { conversation, conversationPage } from "@/test/sidebarMockHelpers";

import { WorkflowDetailPage } from "./WorkflowDetailPage";

vi.mock("@/hooks/useWorkflow", async (importOriginal) => {
  const actual = await importOriginal<typeof WorkflowHooks>();
  return {
    ...actual,
    useWorkflow: vi.fn(),
    useWorkflowRuns: vi.fn(),
    useWorkflowRunDescendants: vi.fn(),
  };
});
vi.mock("@/hooks/useSessionJournal", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionJournalHooks>()),
  useSessionJournal: vi.fn(),
}));
vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: vi.fn() }));
vi.mock("@/hooks/useSidebarData", async (importOriginal) => ({
  ...(await importOriginal<typeof SidebarDataModule>()),
  useOptionalLoadedConversations: vi.fn(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
// The real chat has its own stream/composer tests; this page owns its placement and session link.
vi.mock("@/components/workflows/WorkflowPlanningChat", () => ({
  WorkflowPlanningChat: ({
    workflow,
    onClose,
    expanded,
    onExpandedChange,
  }: {
    workflow: WorkflowDetail;
    onClose: () => void;
    expanded?: boolean;
    onExpandedChange?: (expanded: boolean) => void;
  }) => (
    <div data-testid="planning-chat" data-session={workflow.planning_session_id}>
      {onExpandedChange && (
        <button type="button" onClick={() => onExpandedChange(!expanded)}>
          {expanded ? "Collapse planning chat" : "Expand planning chat"}
        </button>
      )}
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
  return { ...actual, getSession: vi.fn(), interrupt: vi.fn(), sendUserMessage: vi.fn() };
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
// The automations list has its own tests; here it names the workflow it lists.
vi.mock("@/components/workflows/WorkflowAutomations", () => ({
  WorkflowAutomations: ({ workflowId }: { workflowId: string }) => (
    <div data-testid="workflow-automations-section" data-workflow={workflowId} />
  ),
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
    content: { status: value, text: "x", graph_revision: "graph-0" },
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
    planGraphRevision: "graph-0",
    sessionId,
    title,
    startedAt: Date.parse("2026-11-12T15:04:00Z") / 1000,
    sessionStatus: "idle" as const,
    isCurrent: true,
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

function mockDescendantPages(pages: WorkflowRun[][] | null) {
  vi.mocked(useWorkflowRunDescendants).mockImplementation(
    (_workflowId, _run, enabled) =>
      ({
        data:
          enabled && pages
            ? { pages: pages.map((runs) => ({ runs, nextCursor: null })), pageParams: [] }
            : undefined,
        hasNextPage: false,
        isFetching: false,
        isError: false,
        fetchNextPage: vi.fn(),
        refetch: vi.fn(),
      }) as unknown as ReturnType<typeof useWorkflowRunDescendants>,
  );
}

function mockRuns(runs: WorkflowRun[], state: Record<string, unknown> = {}) {
  mockDescendantPages(null);
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

function mockSavedWorkflows(workflows: SavedWorkflow[]) {
  vi.mocked(useSavedWorkflows).mockReturnValue({
    workflows,
    isLoading: false,
    isError: false,
    refresh: vi.fn(),
    pagination: { hasMore: false, loadingMore: false, loadMore: vi.fn() },
  });
}

const deleteWorkflow = vi.fn();

function renderPage(client = new QueryClient()) {
  // A run's play and stop buttons refresh the runs through the query client.
  return render(<WorkflowDetailPage />, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    ),
  });
}

describe("WorkflowDetailPage", () => {
  beforeEach(() => {
    vi.mocked(useIsMobileViewport).mockReturnValue(false);
    vi.mocked(useServerInfo).mockReturnValue("loading");
    vi.mocked(useOptionalLoadedConversations).mockReturnValue(undefined);
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
      retry: vi.fn(),
    });
    vi.mocked(useDeleteWorkflow).mockReturnValue({
      mutate: deleteWorkflow,
      isPending: false,
    } as unknown as ReturnType<typeof useDeleteWorkflow>);
  });
  afterEach(cleanup);

  it.each([false, true])(
    "saves a draft and later edits after closing the planner (mobile=%s)",
    async (mobile) => {
      vi.mocked(useIsMobileViewport).mockReturnValue(mobile);
      vi.mocked(useServerInfo).mockReturnValue(FALLBACK_SERVER_INFO);
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
        content: { steps: WORKFLOW.steps },
      };
      vi.mocked(useSessionJournal).mockReturnValue({
        entries: [proposal],
        isLoading: false,
        error: null,
        hasOlder: false,
        loadingOlder: false,
        loadOlder: vi.fn(),
        retry: vi.fn(),
      });
      renderPage();

      expect(await screen.findByText("Draft")).toBeInTheDocument();
      expect(screen.getByTestId("workflow-steps-graph")).toHaveTextContent(
        "Explore the failures, Fix the test",
      );
      expect(screen.getByTestId("planning-chat")).toHaveAttribute("data-session", planner);
      expect(screen.getByTestId("workflow-detail-run")).toBeDisabled();
      expect(screen.getByTestId("workflow-detail-create-automation")).toBeDisabled();
      expect(screen.queryByTestId("workflow-automations-section")).toBeNull();
      if (mobile) {
        fireEvent.click(screen.getByRole("button", { name: "Close planning chat" }));
        expect(screen.queryByTestId("planning-chat")).toBeNull();
      }
      fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t save the plan");
      expect(screen.getByTestId("workflow-detail-run")).toBeDisabled();

      rejectSave = false;
      fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
      await waitFor(() => expect(screen.getByTestId("workflow-detail-run")).toBeEnabled());
      expect(stored.steps).toEqual(WORKFLOW.steps);
      if (mobile) expect(screen.queryByTestId("planning-chat")).toBeNull();
      else expect(screen.getByTestId("planning-chat")).toBeInTheDocument();
      expect(screen.queryByText("Draft")).toBeNull();
      expect(screen.getByRole("status")).toHaveTextContent("Saved plan");
      expect(screen.getByTestId("workflow-automations-section")).toHaveAttribute(
        "data-workflow",
        WORKFLOW.id,
      );

      cleanup();
      renderPage();
      await screen.findByTestId("workflow-detail-title");
      fireEvent.click(screen.getByRole("button", { name: "Edit plan with agent" }));
      expect(screen.getByTestId("planning-chat")).toHaveAttribute("data-session", planner);
      expect(screen.getByTestId("workflow-detail-run")).toBeEnabled();
      const edited = [
        ...WORKFLOW.steps,
        { id: "report", name: "Report", goal: null, depends_on: ["fix"] },
      ];
      proposal.content = { steps: edited };
      fireEvent.click(screen.getByRole("button", { name: "Close planning chat" }));
      fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
      await waitFor(() => expect(stored.steps).toEqual(edited));
    },
  );

  it("expands the planner over the page in place, on wide screens only", () => {
    mockWorkflow({ data: { ...WORKFLOW, steps: [], is_draft: true } });
    mockRuns([]);
    const { rerender } = renderPage();
    const setPhone = (phone: boolean) => {
      vi.mocked(useIsMobileViewport).mockReturnValue(phone);
      rerender(<WorkflowDetailPage />);
    };
    const page = screen.getByTestId("workflow-detail-page");
    const planner = screen.getByRole("complementary", { name: "Workflow planning chat" });
    const panel = screen.getByTestId("workflow-planner-panel");

    fireEvent.click(screen.getByRole("button", { name: "Expand planning chat" }));
    expect(panel).toHaveClass("absolute", "inset-0");
    expect(page).toHaveClass("invisible");
    // The aside still reserves its width, so the hidden page doesn't reflow.
    expect(planner).toHaveClass("w-[42%]");
    expect(planner).not.toHaveClass("absolute");
    fireEvent.click(screen.getByRole("button", { name: "Collapse planning chat" }));
    expect(panel).not.toHaveClass("absolute");
    expect(page).not.toHaveClass("invisible");

    // Every close path, including the phone sheet after the window narrows,
    // reopens the planner beside the page.
    const closeButton = () =>
      fireEvent.click(screen.getByRole("button", { name: "Close planning chat" }));
    const dismissSheet = () =>
      fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    for (const [phone, close] of [
      [false, closeButton],
      [true, closeButton],
      [true, dismissSheet],
    ] as const) {
      fireEvent.click(screen.getByRole("button", { name: "Expand planning chat" }));
      setPhone(phone);
      // Phones already show the planner as a near-full-screen sheet.
      if (phone) expect(screen.queryByRole("button", { name: "Expand planning chat" })).toBeNull();
      close();
      expect(screen.queryByTestId("planning-chat")).toBeNull();
      setPhone(false);
      fireEvent.click(screen.getByRole("button", { name: "Create plan" }));
      expect(page).not.toHaveClass("invisible");
    }
  });

  it("opens the planning chat ready to type, and flashes it when pressed while open", () => {
    const planner = "e".repeat(32);
    mockWorkflow({ data: { ...WORKFLOW, planning_session_id: planner } });
    mockRuns([]);
    // Pressed before the planning session loads, the request waits for its composer.
    vi.mocked(useSession).mockReturnValue({ session: null, isLoading: true, error: null });
    vi.useFakeTimers();
    try {
      const { rerender } = renderPage();
      const edit = screen.getByRole("button", { name: "Edit plan with agent" });

      fireEvent.click(edit);
      expect(screen.getByTestId("planning-chat")).toHaveAttribute("data-session", planner);
      expect(useChatStore.getState().sideChatComposers[planner]?.autoFocus).toBe(true);
      expect(screen.queryByTestId("workflow-planning-chat-flash")).toBeNull();

      // The real composer drops the request as it takes focus.
      useChatStore.getState().clearSideChatComposer(planner);
      vi.mocked(useSession).mockReturnValue({
        session: { id: planner, archived: false, labels: {} } as Session,
        isLoading: false,
        error: null,
      });
      rerender(<WorkflowDetailPage />);
      fireEvent.click(edit);
      expect(useChatStore.getState().sideChatComposers[planner]?.autoFocus).toBe(true);
      expect(screen.getByTestId("workflow-planning-chat-flash")).toBeInTheDocument();
      act(() => vi.runOnlyPendingTimers());
      expect(screen.queryByTestId("workflow-planning-chat-flash")).toBeNull();
    } finally {
      useChatStore.getState().clearSideChatComposer(planner);
      vi.useRealTimers();
    }
  });

  it.each([
    ["archived", { session: { archived: true, labels: {} } as Session, error: null }],
    ["deleted", { session: null, error: new ApiError("Session not found", 404, null) }],
  ])("queues no composer focus for a %s planning session", (_, state) => {
    const planner = "f".repeat(32);
    mockWorkflow({ data: { ...WORKFLOW, planning_session_id: planner } });
    mockRuns([]);
    vi.mocked(useSession).mockReturnValue({ ...state, isLoading: false });
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Edit plan with agent" }));
    expect(screen.getByTestId("planning-chat")).toBeInTheDocument();
    expect(useChatStore.getState().sideChatComposers[planner]).toBeUndefined();
  });

  it("saves a proposal's description with its steps, but not over a later edit", async () => {
    const actual = await vi.importActual<typeof WorkflowHooks>("@/hooks/useWorkflow");
    vi.mocked(useWorkflow).mockImplementation(actual.useWorkflow);
    mockRuns([]);
    let now = 100;
    let stored: WorkflowDetail = {
      ...WORKFLOW,
      description: "Pull the board's open tickets and group them",
      steps: [],
      is_draft: true,
      planning_session_id: "a".repeat(32),
      updated_at: now,
    };
    const patches: unknown[] = [];
    vi.mocked(authenticatedFetch).mockImplementation(async (_path, options) => {
      if (options?.method === "PATCH") {
        const body = JSON.parse(String(options.body));
        patches.push(body);
        now += 100;
        // As the server does, a description keeps the source it was saved with.
        stored = {
          ...stored,
          steps: body.steps,
          description: body.description ?? stored.description,
          description_source:
            body.description === undefined
              ? stored.description_source
              : (body.description_source ?? null),
          is_draft: false,
          updated_at: now,
        };
      }
      return new Response(JSON.stringify(stored));
    });
    const proposal: JournalEntry = {
      id: "proposal",
      position: 1,
      created_at: 150,
      scheduled_task_run_id: null,
      step: null,
      author: { type: "agent" },
      kind: "plan",
      content: { steps: WORKFLOW.steps, description: WORKFLOW.description },
    };
    let entries = [proposal];
    vi.mocked(useSessionJournal).mockImplementation(() => ({
      entries,
      isLoading: false,
      error: null,
      hasOlder: false,
      loadingOlder: false,
      loadOlder: vi.fn(),
      retry: vi.fn(),
    }));
    const description = () => screen.getByTestId("workflow-detail-description").textContent;
    const saveAndReload = async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
      await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Saved plan"));
      cleanup();
      renderPage();
      await screen.findByTestId("workflow-detail-title");
    };
    renderPage();

    // The draft's description gives way to the proposal's until it saves with the steps.
    await screen.findByTestId("workflow-detail-title");
    expect(description()).toBe(WORKFLOW.description);
    await saveAndReload();
    // Each new description names the one it replaces, so the server keeps a newer one.
    expect(patches).toEqual([
      {
        steps: WORKFLOW.steps,
        description: WORKFLOW.description,
        expected_description: "Pull the board's open tickets and group them",
        description_source: "proposal",
      },
    ]);
    expect(description()).toBe(WORKFLOW.description);
    expect(screen.getByRole("status")).toHaveTextContent("Saved plan");

    // A newer proposal can revise the description alone.
    const revision = {
      ...proposal,
      id: "revision",
      position: 2,
      created_at: 250,
      content: { steps: WORKFLOW.steps, description: "Fixes one flaky test." },
    };
    entries = [proposal, revision];
    cleanup();
    renderPage();
    await screen.findByTestId("workflow-detail-title");
    expect(description()).toBe("Fixes one flaky test.");
    expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");
    await saveAndReload();
    expect(patches[1]).toEqual({
      steps: WORKFLOW.steps,
      description: "Fixes one flaky test.",
      expected_description: WORKFLOW.description,
      description_source: "revision",
    });

    // An edit in the same second as the newest proposal stands; Save plan applies only its steps.
    const edited = [
      ...WORKFLOW.steps,
      { id: "report", name: "Report", goal: null, depends_on: [] },
    ];
    entries = [
      proposal,
      revision,
      {
        ...proposal,
        id: "report",
        position: 3,
        created_at: 400,
        content: { steps: edited, description: "Fixes flaky tests and reports them." },
      },
    ];
    now = 400;
    stored = {
      ...stored,
      description: "Edited by hand",
      description_source: null,
      updated_at: now,
    };
    cleanup();
    renderPage();
    await screen.findByTestId("workflow-detail-title");
    expect(description()).toBe("Edited by hand");
    expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");
    await saveAndReload();
    expect(patches[2]).toEqual({ steps: edited });
    expect(description()).toBe("Edited by hand");
  });

  it("keeps a newer proposal's description unsaved when an older save lands after it", async () => {
    const actual = await vi.importActual<typeof WorkflowHooks>("@/hooks/useWorkflow");
    vi.mocked(useWorkflow).mockImplementation(actual.useWorkflow);
    mockRuns([]);
    let stored: WorkflowDetail = {
      ...WORKFLOW,
      steps: [],
      is_draft: true,
      planning_session_id: "a".repeat(32),
      updated_at: 100,
    };
    const patches: { description?: string }[] = [];
    let releaseFirstSave = () => {};
    vi.mocked(authenticatedFetch).mockImplementation(async (_path, options) => {
      if (options?.method === "PATCH") {
        const body = JSON.parse(String(options.body));
        patches.push(body);
        if (patches.length === 1)
          await new Promise<void>((resolve) => {
            releaseFirstSave = resolve;
          });
        stored = {
          ...stored,
          steps: body.steps,
          description: body.description ?? stored.description,
          description_source:
            body.description === undefined
              ? stored.description_source
              : (body.description_source ?? null),
          is_draft: false,
          updated_at: 300 + patches.length,
        };
      }
      return new Response(JSON.stringify(stored));
    });
    const first: JournalEntry = {
      id: "first",
      position: 1,
      created_at: 150,
      scheduled_task_run_id: null,
      step: null,
      author: { type: "agent" },
      kind: "plan",
      content: { steps: WORKFLOW.steps, description: "Fixes one flaky test." },
    };
    const journal = {
      entries: [first],
      isLoading: false,
      error: null,
      hasOlder: false,
      loadingOlder: false,
      loadOlder: vi.fn(),
      retry: vi.fn(),
    };
    vi.mocked(useSessionJournal).mockImplementation(() => journal);
    const { rerender } = renderPage();
    await screen.findByTestId("workflow-detail-title");

    // The agent revises only the description while the first proposal's save is in flight,
    // and that save lands after the revision was written.
    fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
    await waitFor(() => expect(patches).toHaveLength(1));
    journal.entries = [
      first,
      {
        ...first,
        id: "second",
        position: 2,
        created_at: 200,
        content: { steps: WORKFLOW.steps, description: "Fixes flaky tests." },
      },
    ];
    rerender(<WorkflowDetailPage />);
    await act(async () => releaseFirstSave());

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes"));
    const description = () => screen.getByTestId("workflow-detail-description").textContent;
    expect(description()).toBe("Fixes flaky tests.");

    // Reopened, the page still tells the newer proposal from the one it saved.
    cleanup();
    renderPage();
    await screen.findByTestId("workflow-detail-title");
    expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");
    expect(description()).toBe("Fixes flaky tests.");
    fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Saved plan"));
    expect(patches.map((patch) => patch.description)).toEqual([
      "Fixes one flaky test.",
      "Fixes flaky tests.",
    ]);

    // Another client's description from the same second as that save stands after a refetch.
    stored = { ...stored, description: "Edited elsewhere.", description_source: null };
    cleanup();
    renderPage();
    await screen.findByTestId("workflow-detail-title");
    expect(description()).toBe("Edited elsewhere.");
    expect(screen.getByRole("status")).toHaveTextContent("Saved plan");
  });

  describe("with a description saved from an older proposal", () => {
    const saved: JournalEntry = {
      id: "saved",
      position: 1,
      created_at: 150,
      scheduled_task_run_id: null,
      step: null,
      author: { type: "agent" },
      kind: "plan",
      content: { steps: WORKFLOW.steps, description: "Fixes one flaky test." },
    };
    const edited = [
      ...WORKFLOW.steps,
      { id: "report", name: "Report", goal: null, depends_on: [] },
    ];
    const description = () => screen.getByTestId("workflow-detail-description").textContent;

    // The server's answers to where an entry sits in the journal.
    const placedAfter = (position: number) =>
      Promise.resolve(new Response(JSON.stringify({ object: "list", data: [{ position }] })));
    const notInJournal = () =>
      Promise.resolve(
        new Response(JSON.stringify({ error: { code: "stale_cursor", message: "stale_cursor" } }), {
          status: 400,
        }),
      );
    function serve(stored: () => WorkflowDetail, place: () => Promise<Response>) {
      vi.mocked(authenticatedFetch).mockImplementation(async (path) =>
        String(path).includes("/journal?") ? place() : new Response(JSON.stringify(stored())),
      );
    }

    async function renderWith(newer: JournalEntry, stored: () => WorkflowDetail) {
      const actual = await vi.importActual<typeof WorkflowHooks>("@/hooks/useWorkflow");
      vi.mocked(useWorkflow).mockImplementation(actual.useWorkflow);
      mockRuns([]);
      vi.mocked(useSessionJournal).mockReturnValue({
        entries: [saved, newer],
        isLoading: false,
        error: null,
        hasOlder: false,
        loadingOlder: false,
        loadOlder: vi.fn(),
        retry: vi.fn(),
      });
      vi.mocked(authenticatedFetch).mockImplementation(
        async () => new Response(JSON.stringify(stored())),
      );
      renderPage();
      await screen.findByTestId("workflow-detail-title");
    }

    it("keeps that wording when the user restores it by hand over a newer proposal", async () => {
      let stored: WorkflowDetail = {
        ...WORKFLOW,
        description: "Fixes one flaky test.",
        description_source: "saved",
        planning_session_id: "a".repeat(32),
        updated_at: 200,
      };
      const newer = {
        ...saved,
        id: "newer",
        position: 2,
        created_at: 250,
        content: { steps: WORKFLOW.steps, description: "Fixes flaky tests." },
      };
      await renderWith(newer, () => stored);
      vi.mocked(authenticatedFetch).mockImplementation(async (_path, options) => {
        if (options?.method === "PATCH") {
          // The editor writes the description alone, which clears its source on the server.
          const body = JSON.parse(String(options.body));
          stored = {
            ...stored,
            description: body.description,
            description_source: null,
            updated_at: 300,
          };
        }
        return new Response(JSON.stringify(stored));
      });
      expect(description()).toBe("Fixes flaky tests.");

      fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
      fireEvent.change(screen.getByRole("textbox", { name: "Workflow description" }), {
        target: { value: "Fixes one flaky test." },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(description()).toBe("Fixes one flaky test."));
      expect(screen.getByRole("status")).toHaveTextContent("Saved plan");
      cleanup();
      renderPage();
      await screen.findByTestId("workflow-detail-title");
      expect(description()).toBe("Fixes one flaky test.");
      expect(screen.getByRole("status")).toHaveTextContent("Saved plan");
    });

    it("leaves the workflow runnable when an older server saves only the steps", async () => {
      // An older server returns no description source and refuses the conditional fields.
      let stored: WorkflowDetail = {
        ...WORKFLOW,
        description: "Fixes one flaky test.",
        planning_session_id: "a".repeat(32),
        updated_at: 200,
      };
      const newer = {
        ...saved,
        id: "newer",
        position: 2,
        created_at: 250,
        content: { steps: edited, description: "Fixes flaky tests." },
      };
      await renderWith(newer, () => stored);
      vi.mocked(authenticatedFetch).mockImplementation(async (_path, options) => {
        if (options?.method === "PATCH") {
          const body = JSON.parse(String(options.body));
          if ("expected_description" in body) {
            const detail = [{ type: "extra_forbidden", loc: ["body", "expected_description"] }];
            return new Response(JSON.stringify({ detail }), { status: 422 });
          }
          stored = { ...stored, steps: body.steps, updated_at: 300 };
        }
        return new Response(JSON.stringify(stored));
      });
      expect(screen.getByTestId("workflow-detail-run")).toBeDisabled();

      fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
      await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Saved plan"));
      expect(screen.getByTestId("workflow-detail-run")).toBeEnabled();
      expect(description()).toBe("Fixes one flaky test.");
    });

    it("keeps a newer description unsaved when a save that kept the old one lands after it", async () => {
      const actual = await vi.importActual<typeof WorkflowHooks>("@/hooks/useWorkflow");
      vi.mocked(useWorkflow).mockImplementation(actual.useWorkflow);
      mockRuns([]);
      let stored: WorkflowDetail = {
        ...WORKFLOW,
        description: "Fixes one flaky test.",
        planning_session_id: "a".repeat(32),
        updated_at: 100,
      };
      const patches: Record<string, unknown>[] = [];
      let releaseSave = () => {};
      vi.mocked(authenticatedFetch).mockImplementation(async (_path, options) => {
        if (options?.method === "PATCH") {
          const body = JSON.parse(String(options.body));
          patches.push(body);
          await new Promise<void>((resolve) => {
            releaseSave = resolve;
          });
          stored = {
            ...stored,
            steps: body.steps,
            description: body.description ?? stored.description,
            description_source:
              body.description === undefined
                ? stored.description_source
                : (body.description_source ?? null),
            updated_at: 301,
          };
        }
        return new Response(JSON.stringify(stored));
      });
      // The first proposal changes only the steps and keeps the saved wording.
      const first = {
        ...saved,
        id: "first",
        content: { steps: edited, description: "Fixes one flaky test." },
      };
      const journal = {
        entries: [first],
        isLoading: false,
        error: null,
        hasOlder: false,
        loadingOlder: false,
        loadOlder: vi.fn(),
        retry: vi.fn(),
      };
      vi.mocked(useSessionJournal).mockImplementation(() => journal);
      const { rerender } = renderPage();
      await screen.findByTestId("workflow-detail-title");

      fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
      await waitFor(() => expect(patches).toHaveLength(1));
      journal.entries = [
        first,
        {
          ...first,
          id: "second",
          position: 2,
          created_at: 200,
          content: { steps: edited, description: "Fixes flaky tests." },
        },
      ];
      rerender(<WorkflowDetailPage />);
      await act(async () => releaseSave());

      await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes"));
      expect(description()).toBe("Fixes flaky tests.");
      expect(patches[0]).toMatchObject({
        description: "Fixes one flaky test.",
        description_source: "first",
      });
    });

    it("offers a newer proposal when the saved one came from an earlier planning chat", async () => {
      const stored: WorkflowDetail = {
        ...WORKFLOW,
        description: "Fixes one flaky test.",
        description_source: "f".repeat(32),
        planning_session_id: "a".repeat(32),
        updated_at: 500,
      };
      const newer = {
        ...saved,
        id: "newer",
        created_at: 600,
        content: { steps: WORKFLOW.steps, description: "Fixes flaky tests." },
      };
      const actual = await vi.importActual<typeof WorkflowHooks>("@/hooks/useWorkflow");
      vi.mocked(useWorkflow).mockImplementation(actual.useWorkflow);
      mockRuns([]);
      vi.mocked(useSessionJournal).mockReturnValue({
        entries: [newer],
        isLoading: false,
        error: null,
        hasOlder: false,
        loadingOlder: false,
        loadOlder: vi.fn(),
        retry: vi.fn(),
      });
      serve(() => stored, notInJournal);
      renderPage();
      await screen.findByTestId("workflow-detail-title");
      await waitFor(() => expect(description()).toBe("Fixes flaky tests."));
      expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");
    });

    it("offers a newer proposal when the one saved is older than the loaded journal", async () => {
      const actual = await vi.importActual<typeof WorkflowHooks>("@/hooks/useWorkflow");
      vi.mocked(useWorkflow).mockImplementation(actual.useWorkflow);
      mockRuns([]);
      const stored: WorkflowDetail = {
        ...WORKFLOW,
        description: "Fixes one flaky test.",
        description_source: "e".repeat(32),
        planning_session_id: "a".repeat(32),
        // A rename after the newer proposal moved the workflow's last write past it.
        updated_at: 500,
      };
      serve(
        () => stored,
        () => placedAfter(9),
      );
      const page = (hasOlder: boolean) => ({
        entries: [
          {
            ...saved,
            id: "newer",
            position: 150,
            created_at: 300,
            content: { steps: WORKFLOW.steps, description: "Fixes flaky tests." },
          },
        ],
        isLoading: false,
        error: null,
        hasOlder,
        loadingOlder: false,
        loadOlder: vi.fn(),
        retry: vi.fn(),
      });
      vi.mocked(useSessionJournal).mockReturnValue(page(true));
      renderPage();
      await screen.findByTestId("workflow-detail-title");
      await waitFor(() => expect(description()).toBe("Fixes flaky tests."));
      expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");

      // A source the journal doesn't hold came from another planning chat: the workflow's
      // last write is newer than the proposal, so the description stays.
      let answered = false;
      serve(
        () => stored,
        () => notInJournal().finally(() => (answered = true)),
      );
      cleanup();
      vi.mocked(useSessionJournal).mockReturnValue(page(false));
      renderPage();
      await screen.findByTestId("workflow-detail-title");
      await waitFor(() => expect(answered).toBe(true));
      await act(async () => {});
      expect(description()).toBe("Fixes one flaky test.");
      expect(screen.getByRole("status")).toHaveTextContent("Saved plan");
    });

    it("keeps a description saved elsewhere while a stale journal lacks its proposal", async () => {
      const actual = await vi.importActual<typeof WorkflowHooks>("@/hooks/useWorkflow");
      vi.mocked(useWorkflow).mockImplementation(actual.useWorkflow);
      mockRuns([]);
      // Another tab saved proposal "p2" after this tab cached a journal ending at "saved".
      const stored: WorkflowDetail = {
        ...WORKFLOW,
        description: "Fixes flaky tests.",
        description_source: "p2",
        planning_session_id: "a".repeat(32),
        updated_at: 500,
      };
      vi.mocked(useSessionJournal).mockReturnValue({
        entries: [saved],
        isLoading: false,
        error: null,
        hasOlder: true,
        loadingOlder: false,
        loadOlder: vi.fn(),
        retry: vi.fn(),
      });
      const expectSaved = () => {
        expect(description()).toBe("Fixes flaky tests.");
        expect(screen.getByRole("status")).toHaveTextContent("Saved plan");
        expect(screen.queryByRole("button", { name: "Save plan" })).toBeNull();
      };

      // Until the server places "p2", the cached proposal isn't offered.
      let place = () => {};
      const placed = new Response(
        JSON.stringify({ object: "list", data: [{ position: saved.position }] }),
      );
      serve(
        () => stored,
        () =>
          new Promise<Response>((resolve) => {
            place = () => resolve(placed);
          }),
      );
      renderPage();
      await screen.findByTestId("workflow-detail-title");
      expectSaved();
      // Placed after the cached proposal, it keeps the saved description.
      await act(async () => place());
      await act(async () => {});
      expectSaved();
    });
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
        fireEvent.click(screen.getByRole("button", { name: "Edit plan with agent" }));
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

  it("edits the description in place, keeping the draft when a save fails", async () => {
    const actual = await vi.importActual<typeof WorkflowHooks>("@/hooks/useWorkflow");
    vi.mocked(useWorkflow).mockImplementation(actual.useWorkflow);
    mockRuns([]);
    let stored: WorkflowDetail = { ...WORKFLOW };
    const patches: unknown[] = [];
    let rejectSave = true;
    vi.mocked(authenticatedFetch).mockImplementation(async (_path, options) => {
      if (options?.method === "PATCH") {
        patches.push(JSON.parse(String(options.body)));
        if (rejectSave)
          return new Response(
            JSON.stringify({ error: { message: "Try again", type: "server_error" } }),
            { status: 500 },
          );
        stored = {
          ...stored,
          description: (patches.at(-1) as { description: string }).description,
        };
      }
      return new Response(JSON.stringify(stored));
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    try {
      renderPage();
      await screen.findByTestId("workflow-detail-description");
      const editorName = { name: "Workflow description" };

      // An unchanged description closes on Escape without saving.
      fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
      const unchanged = screen.getByRole("textbox", editorName);
      expect(unchanged).toHaveValue(WORKFLOW.description);
      expect(unchanged).toHaveFocus();
      expect((unchanged as HTMLTextAreaElement).selectionStart).toBe(WORKFLOW.description.length);
      fireEvent.keyDown(unchanged, { key: "Escape" });
      expect(screen.queryByRole("textbox", editorName)).toBeNull();

      fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
      const editor = screen.getByRole("textbox", editorName);
      // The shortcut can't save an empty description, just as the button can't.
      fireEvent.change(editor, { target: { value: "   " } });
      fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true });
      expect(patches).toEqual([]);
      fireEvent.change(editor, {
        target: { value: "  Finds flaky tests and fixes the clear ones.  " },
      });
      // Keys that end an IME composition neither cancel nor save.
      fireEvent.keyDown(editor, { key: "Escape", isComposing: true });
      fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true, isComposing: true });
      expect(confirm).not.toHaveBeenCalled();
      expect(patches).toEqual([]);
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      // Browsers blur a control as saving disables it; jsdom doesn't.
      act(() => editor.blur());
      expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t save the description");
      expect(editor).toHaveFocus();
      // Escape asks before discarding the edit.
      fireEvent.keyDown(editor, { key: "Escape" });
      expect(confirm).toHaveBeenCalledOnce();
      expect(editor).toHaveValue("  Finds flaky tests and fixes the clear ones.  ");

      rejectSave = false;
      fireEvent.keyDown(editor, { key: "Enter", metaKey: true });
      await waitFor(() => expect(screen.queryByRole("textbox", editorName)).toBeNull());
      expect(patches).toEqual([
        { description: "Finds flaky tests and fixes the clear ones." },
        { description: "Finds flaky tests and fixes the clear ones." },
      ]);
      expect(screen.getByTestId("workflow-detail-description").textContent).toBe(
        "Finds flaky tests and fixes the clear ones.",
      );
      expect(screen.queryByRole("alert")).toBeNull();

      // Cancel discards an edit once confirmed.
      confirm.mockReturnValue(true);
      fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
      fireEvent.change(screen.getByRole("textbox", editorName), {
        target: { value: "Something else" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(screen.queryByRole("textbox", editorName)).toBeNull();
      expect(screen.getByTestId("workflow-detail-description").textContent).toBe(
        "Finds flaky tests and fixes the clear ones.",
      );
      expect(patches).toHaveLength(2);
    } finally {
      confirm.mockRestore();
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
      {
        ...run("d".repeat(32), "Background run", { started: 1 }, [status("explore", "started")]),
        backgroundWorkRunning: true,
      },
      run("c".repeat(32), "Finished run", { done: 2 }, [
        status("explore", "done"),
        status("fix", "done"),
      ]),
    ]);
    renderPage();

    const [stopped, running, background, finished] = screen.getAllByTestId("workflow-run");
    expect(within(stopped!).getByRole("button", { name: "Continue the plan" })).toBeInTheDocument();
    expect(within(running!).getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(background).toHaveAttribute("data-run-state", "running");
    expect(within(background!).getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(within(finished!).queryByTestId("plan-continue")).toBeNull();
    expect(within(finished!).queryByTestId("plan-stop")).toBeNull();
  });

  it("finishes a run when its final step is done despite earlier statuses and session activity", () => {
    mockWorkflow({});
    const completedRun: WorkflowRun = {
      ...run("a".repeat(32), "Finished run", { done: 1, needsInput: 1 }, [
        status("explore", "needs_input"),
        { ...status("fix", "done"), position: 2 },
      ]),
      sessionStatus: "running",
      backgroundWorkRunning: true,
      planGraphRevision: "graph-0",
    };
    mockRuns([completedRun]);
    const { rerender } = renderPage();
    const row = screen.getByTestId("workflow-run");
    expect(row).toHaveAttribute("data-run-state", "done");
    expect(within(row).getByTestId("workflow-run-state")).toHaveTextContent("Done");
    expect(within(row).queryByTestId("plan-continue")).toBeNull();
    expect(within(row).queryByTestId("plan-stop")).toBeNull();
    expect(screen.getByTestId("workflow-detail-run")).toBeEnabled();

    mockRuns([
      {
        ...completedRun,
        planGraphRevision: undefined,
        progress: { total: 2, started: 0, done: 2, needsInput: 0, failed: 0 },
        statuses: [status("explore", "done"), status("fix", "done")],
      },
    ]);
    rerender(<WorkflowDetailPage />);
    expect(row).toHaveAttribute("data-run-state", "running");
    expect(within(row).getByRole("button", { name: "Stop" })).toBeInTheDocument();

    mockRuns([
      {
        ...completedRun,
        planGraphRevision: undefined,
        sessionStatus: "idle",
        backgroundWorkRunning: false,
        steps: [
          WORKFLOW.steps[0]!,
          { id: "review", name: "Review", goal: null, depends_on: ["explore"] },
          { ...WORKFLOW.steps[1]!, depends_on: ["review"] },
        ],
        progress: { total: 3, started: 0, done: 2, needsInput: 0, failed: 0 },
        statuses: [status("explore", "done"), status("fix", "done")],
      },
    ]);
    rerender(<WorkflowDetailPage />);
    expect(row).toHaveAttribute("data-run-state", "stopped");
    expect(within(row).getByRole("button", { name: "Continue the plan" })).toBeInTheDocument();

    mockRuns([
      {
        ...completedRun,
        planGraphRevision: "graph-2",
        steps: [
          WORKFLOW.steps[0]!,
          { id: "review", name: "Review", goal: null, depends_on: ["explore"] },
          { ...WORKFLOW.steps[1]!, depends_on: ["review"] },
        ],
        progress: { total: 3, started: 0, done: 2, needsInput: 0, failed: 0 },
        statuses: [status("explore", "done"), status("fix", "done")],
      },
    ]);
    rerender(<WorkflowDetailPage />);
    expect(row).toHaveAttribute("data-run-state", "running");
    expect(within(row).getByRole("button", { name: "Stop" })).toBeInTheDocument();

    // A replan keeps the run's ID and can add work after its original last step.
    mockRuns([
      {
        ...completedRun,
        steps: [
          ...completedRun.steps,
          { id: "publish", name: "Publish", goal: null, depends_on: ["fix"] },
        ],
        progress: { total: 3, started: 1, done: 2, needsInput: 0, failed: 0 },
        statuses: [status("explore", "done"), status("fix", "done"), status("publish", "started")],
      },
    ]);
    rerender(<WorkflowDetailPage />);
    expect(row).toHaveAttribute("data-run-state", "running");
    expect(within(row).getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it.each([
    { source: "background work", activity: { background_work_running: true } },
    { source: "child activity", activity: { status: "running" as const } },
  ])("keeps the run, tasks and graph in sync with sidebar $source", ({ activity }) => {
    mockWorkflow({});
    const session = "a".repeat(32);
    const activeRun = run(session, "Fix the upload test", { started: 1 }, [
      status("explore", "started"),
    ]);
    mockRuns([activeRun]);
    vi.mocked(useOptionalLoadedConversations).mockReturnValue({
      ...conversationPage([conversation(session, activity)]).data!,
      pageParams: [undefined],
    });
    const { rerender } = renderPage();
    const row = screen.getByTestId("workflow-run");
    expect(row).toHaveAttribute("data-run-state", "running");
    expect(within(row).getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(within(row).getAllByTestId("tasks-progress-segment")[0]).toHaveAttribute(
      "aria-label",
      "Task 1, Explore the failures: Running",
    );
    fireEvent.click(screen.getByRole("button", { name: "View Fix the upload test on the graph" }));
    expect(screen.getByTestId("workflow-graph-overlay")).toHaveTextContent("explore:running");

    // The live sidebar settles before the next run-list refresh.
    mockRuns([{ ...activeRun, sessionStatus: "running" }]);
    vi.mocked(useOptionalLoadedConversations).mockReturnValue({
      ...conversationPage([conversation(session)]).data!,
      pageParams: [undefined],
    });
    rerender(<WorkflowDetailPage />);
    expect(row).toHaveAttribute("data-run-state", "stopped");
    expect(within(row).getByRole("button", { name: "Continue the plan" })).toBeInTheDocument();
    expect(screen.getByTestId("workflow-graph-overlay")).toHaveTextContent("explore:stopped");
  });

  it.each([false, true])(
    "keeps a superseded run read-only while its session works (completed=%s)",
    async (completed) => {
      mockWorkflow({});
      const session = "a".repeat(32);
      mockRuns([
        {
          ...run(session, "Historical run", { done: completed ? 2 : 0 }, [
            status("explore", completed ? "done" : "started"),
            ...(completed ? [status("fix", "done")] : []),
          ]),
          id: "historical",
          isCurrent: false,
          sessionStatus: "running",
          backgroundWorkRunning: true,
        },
        run(session, "Current run", { started: 1 }, [status("explore", "started")]),
      ]);
      vi.mocked(useOptionalLoadedConversations).mockReturnValue({
        ...conversationPage([conversation(session, { background_work_running: true })]).data!,
        pageParams: [undefined],
      });
      renderPage();
      const [historical, current] = screen.getAllByTestId("workflow-run");
      expect(historical).toHaveAttribute("data-run-state", completed ? "done" : "stopped");
      expect(within(historical!).queryByTestId("plan-stop")).toBeNull();
      expect(within(historical!).queryByTestId("plan-continue")).toBeNull();
      expect(within(current!).getByRole("button", { name: "Stop" })).toBeInTheDocument();
      fireEvent.focus(within(historical!).getAllByTestId("tasks-progress-segment")[0]!);
      expect(await screen.findByTestId("tasks-progress-card")).toHaveTextContent(
        "Explore the failures",
      );
      expect(screen.queryByTestId("tasks-progress-menu")).toBeNull();
      fireEvent.click(within(historical!).getByTestId("run-graph-button"));
      expect(screen.getByTestId("workflow-graph-overlay")).toHaveTextContent(
        `explore:${completed ? "done" : "stopped"}`,
      );
    },
  );

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
        taskActionMessage("retry", "Fix the test"),
        `plan-${session}`,
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

  it("opens a run's session at the question a waiting task's agent waits on", async () => {
    vi.mocked(sendUserMessage).mockClear().mockResolvedValue({ queued: true });
    const session = "a".repeat(32);
    const pendingQuestion = {
      type: "response.elicitation_request",
      elicitation_id: "elicit_fix",
      params: {
        mode: "form",
        message: "Waiting for you on Fix the test",
        phase: "workflow_request_input",
        journal_step: "fix",
      },
    };
    vi.mocked(getSession).mockResolvedValue({ pendingElicitations: [pendingQuestion] } as never);
    mockWorkflow({});
    mockRuns([
      run(session, "Fix the upload test", { done: 1, needsInput: 1 }, [
        status("explore", "done"),
        status("fix", "needs_input"),
      ]),
    ]);
    renderPage();

    const openCard = async () => {
      fireEvent.focus(
        within(screen.getByTestId("workflow-run")).getAllByTestId("tasks-progress-segment")[1]!,
      );
      fireEvent.pointerDown(await screen.findByTestId("tasks-progress-menu"), { button: 0 });
      fireEvent.click(screen.getByTestId("tasks-progress-proceed"));
    };
    await openCard();
    await waitFor(() => expect(navigate).toHaveBeenCalled());
    const [path, search] = String(navigate.mock.calls[0]![0]).split("?");
    expect(path).toBe(`/c/${session}`);
    expect(new URLSearchParams(search).get("message")).toBe("elicit_fix");
    expect(sendUserMessage).not.toHaveBeenCalled();

    // Once the question is gone, Continue asks the agent to go ahead.
    vi.mocked(getSession).mockResolvedValue({ pendingElicitations: [] } as never);
    navigate.mockClear();
    await waitFor(() => expect(screen.queryByTestId("tasks-progress-card")).toBeNull());
    await openCard();
    await waitFor(() =>
      expect(sendUserMessage).toHaveBeenCalledWith(
        session,
        expect.stringContaining('continue the task "Fix the test"'),
        `plan-${session}`,
      ),
    );
    expect(navigate).not.toHaveBeenCalled();
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

  it("lists the workflow runs below a run, saying who started the ones further down", () => {
    mockWorkflow({});
    const calledId = "c".repeat(32);
    // The server names the workflows you own, loaded in the saved list or not.
    mockSavedWorkflows([]);
    const root = "a".repeat(32);
    const ready = "b".repeat(32);
    mockRuns([
      {
        ...run(root, "Land stack", { done: 1 }),
        sessionStatus: "running",
        descendantRuns: [
          {
            ...run(ready, "Ready #1", { done: 1 }, [status("explore", "done")]),
            workflowId: calledId,
            workflowName: "Ready a PR",
            startedBy: { sessionId: root, title: "Land stack" },
          },
          {
            ...run("d".repeat(32), "Fix flaky test", { done: 1 }, [status("explore", "done")]),
            workflowId: "e".repeat(32),
            workflowName: null,
            startedBy: { sessionId: ready, title: "" },
          },
        ],
      },
      {
        ...run("f".repeat(32), "Earlier stack", { done: 2 }),
        descendantRuns: [run("0".repeat(32), "Ready #0", { done: 2 })],
      },
    ]);
    renderPage();

    const [current, earlier] = screen.getAllByTestId("workflow-run-descendants-toggle");
    // A run still going shows the runs below it; a finished one starts folded.
    expect(current).toHaveTextContent("Ran 2 workflows");
    expect(current).toHaveAttribute("aria-expanded", "true");
    expect(earlier).toHaveTextContent("Ran 1 workflow");
    expect(earlier).toHaveAttribute("aria-expanded", "false");
    const rows = within(screen.getByTestId("workflow-run-descendants")).getAllByTestId(
      "workflow-run",
    );
    expect(rows.map((row) => row.querySelector("a")?.textContent)).toEqual([
      "Ready #1",
      "Fix flaky test",
    ]);
    // Only a run further down says who started it; only a run of your workflow
    // opens that workflow or takes actions.
    expect(within(rows[0]!).queryByTestId("workflow-run-started-by")).toBeNull();
    const startedBy = within(rows[1]!).getByTestId("workflow-run-started-by");
    expect(startedBy).toHaveTextContent("Started by Untitled session");
    expect(startedBy).toHaveAttribute("href", `/c/${ready}`);
    expect(within(rows[0]!).getByTestId("run-workflow-button")).toHaveAttribute(
      "href",
      `/workflows/${calledId}`,
    );
    expect(within(rows[1]!).queryByTestId("run-workflow-button")).toBeNull();
    expect(within(rows[0]!).getByRole("button", { name: "Continue the plan" })).toBeInTheDocument();
    expect(within(rows[1]!).queryByRole("button", { name: "Continue the plan" })).toBeNull();

    fireEvent.click(earlier!);
    expect(earlier).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByTestId("workflow-run-descendants")).toHaveLength(2);
  });

  it("shows the rest of the workflow runs below a run on request", () => {
    mockWorkflow({});
    mockSavedWorkflows([]);
    const listed = {
      ...run("a".repeat(32), "Land stack", { done: 1 }),
      descendantRuns: [run("b".repeat(32), "Ready #1", {})],
      descendantRunCount: 3,
      descendantRunsCursor: "b".repeat(32),
    };
    mockRuns([listed]);
    mockDescendantPages([
      [run("c".repeat(32), "Ready #2", {})],
      [run("d".repeat(32), "Ready #3", {})],
    ]);
    renderPage();

    const toggle = screen.getByTestId("workflow-run-descendants-toggle");
    expect(toggle).toHaveTextContent("Ran 3 workflows");
    fireEvent.click(toggle);
    const list = screen.getByTestId("workflow-run-descendants");
    const titles = () =>
      within(list)
        .getAllByTestId("workflow-run")
        .map((row) => row.querySelector("a")?.textContent);
    expect(titles()).toEqual(["Ready #1"]);
    expect(useWorkflowRunDescendants).toHaveBeenLastCalledWith(WORKFLOW.id, listed, false);

    fireEvent.click(within(list).getByTestId("workflow-run-descendants-more"));
    expect(useWorkflowRunDescendants).toHaveBeenLastCalledWith(WORKFLOW.id, listed, true);
    expect(titles()).toEqual(["Ready #1", "Ready #2", "Ready #3"]);
    expect(within(list).queryByTestId("workflow-run-descendants-more")).toBeNull();
  });

  it("links a run another session started back to that session", () => {
    mockWorkflow({});
    const starter = "a".repeat(32);
    mockRuns([
      { ...run("b".repeat(32), "Ready #1", {}), startedBy: { sessionId: starter, title: "Stack" } },
      run("c".repeat(32), "Ran directly", {}),
    ]);
    renderPage();

    const [started, direct] = screen.getAllByTestId("workflow-run");
    const link = within(started!).getByTestId("workflow-run-started-by");
    expect(link).toHaveTextContent("Started by Stack");
    expect(link).toHaveAttribute("href", `/c/${starter}`);
    expect(within(direct!).queryByTestId("workflow-run-started-by")).toBeNull();
    expect(screen.queryByTestId("workflow-run-descendants-toggle")).toBeNull();
  });

  it("lists the workflow runs below a session's current run in its Tasks tab", () => {
    mockSavedWorkflows([]);
    const session = "a".repeat(32);
    mockRuns([
      {
        ...run(session, "Land stack", {}),
        id: "plan-earlier",
        isCurrent: false,
        descendantRuns: [run("b".repeat(32), "Ready #0", {})],
      },
      { ...run(session, "Land stack", {}), descendantRuns: [run("c".repeat(32), "Ready #1", {})] },
    ]);
    const tasksTab = () =>
      render(<SessionDescendantRuns sessionId={session} workflowId={WORKFLOW.id} />, {
        wrapper: ({ children }) => (
          <QueryClientProvider client={new QueryClient()}>
            <MemoryRouter>{children}</MemoryRouter>
          </QueryClientProvider>
        ),
      });
    tasksTab();
    expect(useWorkflowRuns).toHaveBeenCalledWith(WORKFLOW.id, session);
    fireEvent.click(screen.getByTestId("workflow-run-descendants-toggle"));
    const rows = within(screen.getByTestId("tasks-descendant-runs")).getAllByTestId("workflow-run");
    expect(rows.map((row) => row.querySelector("a")?.textContent)).toEqual(["Ready #1"]);
    cleanup();

    mockRuns([run(session, "Land stack", {})]);
    tasksTab();
    expect(screen.queryByTestId("tasks-descendant-runs")).toBeNull();
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

  it.each(["loading", false, true, undefined] as const)(
    "keeps workflow actions available and gates automation creation (%s)",
    (automations) => {
      vi.mocked(useServerInfo).mockReturnValue(
        automations === "loading"
          ? "loading"
          : {
              ...FALLBACK_SERVER_INFO,
              automations_enabled: automations,
            },
      );
      mockWorkflow({});
      mockRuns([]);
      const { rerender } = renderPage();

      fireEvent.click(screen.getByTestId("workflow-detail-run"));
      expect(JSON.parse(screen.getByTestId("workflow-session-dialog").textContent!)).toEqual({
        kind: "run",
        workflow: { id: WORKFLOW.id, name: WORKFLOW.name, description: WORKFLOW.description },
      });
      if (automations === "loading" || automations === false) {
        expect(screen.queryByTestId("workflow-detail-create-automation")).toBeNull();
        expect(screen.queryByTestId("new-automation-dialog")).toBeNull();
        expect(screen.queryByTestId("workflow-automations-section")).toBeNull();
      } else {
        expect(screen.getByTestId("workflow-automations-section")).toHaveAttribute(
          "data-workflow",
          WORKFLOW.id,
        );
        fireEvent.click(screen.getByTestId("workflow-detail-create-automation"));
        expect(screen.getByTestId("new-automation-dialog")).toHaveTextContent("Triage flaky tests");
      }
      fireEvent.click(screen.getByRole("button", { name: "Edit plan with agent" }));
      expect(screen.getByTestId("planning-chat")).toBeInTheDocument();

      // A capability refresh can disable an already-open creation dialog.
      vi.mocked(useServerInfo).mockReturnValue({
        ...FALLBACK_SERVER_INFO,
        automations_enabled: false,
      });
      rerender(<WorkflowDetailPage />);
      expect(screen.queryByTestId("new-automation-dialog")).toBeNull();
      expect(screen.queryByTestId("workflow-automations-section")).toBeNull();

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
    },
  );

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
