// Tests for the scheduled-task DETAIL page (`/tasks/:taskId`): header + prompt
// + configuration + run-history rendering from mocked hooks, the header actions
// (Run now fires the mutation + shows a toast, delete navigates back, edit opens the dialog),
// and the run-row rendering rules: the LEFT status-icon column (failed triangle
// > skipped calendar-off > running spinner > succeeded-unread brand-accent dot > succeeded-read grey dot),
// duration, errorCode messages, and the whole-row click affordance (a run with a
// conversation is a link; a skipped run is not) — never a fabricated summary.
//
// All data hooks are mocked at their seam; the edit dialog is stubbed so we
// only assert it opened. useParams/useNavigate come from `@/lib/routing`.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetailPage } from "./TaskDetailPage";
import { TooltipProvider } from "@/components/ui/tooltip";
import * as hooks from "@/hooks/useScheduledTasks";
import {
  markConversationSeen,
  markConversationUnread,
  resetReadStateForTests,
  seedReadState,
} from "@/hooks/useUnseenConversations";
import type * as UnseenModule from "@/hooks/useUnseenConversations";
import { ScheduledTaskApiError } from "@/lib/scheduledTasksApi";
import type * as RoutingModule from "@/lib/routing";
import type { ScheduledTask, ScheduledTaskRun } from "@/lib/scheduledTasksApi";

const navigate = vi.fn();
let routeParams = { taskId: "st_1" };
vi.mock("@/lib/routing", async (importOriginal) => {
  const actual = await importOriginal<typeof RoutingModule>();
  return {
    ...actual,
    useNavigate: () => navigate,
    useParams: () => routeParams,
  };
});

vi.mock("@/hooks/useScheduledTasks", () => ({
  useScheduledTask: vi.fn(),
  useScheduledTaskRuns: vi.fn(),
  useUpdateScheduledTask: vi.fn(),
  useDeleteScheduledTask: vi.fn(),
  useRunScheduledTaskNow: vi.fn(),
}));

const showToast = vi.fn();
vi.mock("@/components/ui/toast", () => ({ showToast: (...args: unknown[]) => showToast(...args) }));

// Agent / host catalogs — return simple lists so labels resolve.
const defaultAgentCatalog = (_options?: { pinnedAgentIds?: string[] }) => ({
  data: [{ id: "ag_1", name: "polly", display_name: "Polly" }],
});
const useAvailableAgents = vi.fn(defaultAgentCatalog);
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: (options?: { pinnedAgentIds?: string[] }) => useAvailableAgents(options),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [{ host_id: "h_1", name: "My Laptop" }] }),
}));

// The real read-state mirror, so unread verdicts come from actual state. Only
// seedRunUnreadBaseline is wrapped, to count auto-marks.
const { seedRunUnreadBaseline } = vi.hoisted(() => ({ seedRunUnreadBaseline: vi.fn() }));
vi.mock("@/hooks/useUnseenConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof UnseenModule>();
  return {
    ...actual,
    seedRunUnreadBaseline: (conversationId: string, updatedAt: number) => {
      seedRunUnreadBaseline(conversationId, updatedAt);
      actual.seedRunUnreadBaseline(conversationId, updatedAt);
    },
  };
});

// Freeze the ticking clock so relative next-run text is deterministic.
vi.mock("@/hooks/useNow", () => ({
  useNow: () => new Date("2026-07-27T12:00:00Z"),
}));

// Stub the edit dialog to a marker that reports open + the task it edits.
vi.mock("@/components/scheduled/CreateScheduledTaskDialog", () => ({
  CreateScheduledTaskDialog: ({
    open,
    editingTask,
  }: {
    open: boolean;
    editingTask?: ScheduledTask | null;
  }) =>
    open ? <div data-testid="edit-dialog-open" data-editing-id={editingTask?.id ?? ""} /> : null,
}));

function task(overrides: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: "st_1",
    name: "Nightly triage",
    prompt: "Triage the inbox and summarize.",
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

function run(overrides: Partial<ScheduledTaskRun> = {}): ScheduledTaskRun {
  return {
    id: "run_1",
    scheduledTaskId: "st_1",
    status: "succeeded",
    scheduledAt: 1_700_000_000,
    conversationId: null,
    firedAt: 1_700_000_000,
    finishedAt: 1_700_000_102,
    errorCode: null,
    conversationUpdatedAt: 1_700_000_500,
    conversationStatus: "idle",
    viewerLastSeen: null,
    viewerUnread: null,
    ...overrides,
  };
}

const updateMutate = vi.fn();
const deleteMutate = vi.fn();
const runNowMutate = vi.fn();

const refetchTask = vi.fn();
function setTask(
  data: ScheduledTask | undefined,
  state: { isLoading?: boolean; error?: Error } = {},
) {
  vi.mocked(hooks.useScheduledTask).mockReturnValue({
    data,
    isLoading: state.isLoading ?? false,
    isError: state.error !== undefined,
    error: state.error ?? null,
    refetch: refetchTask,
  } as unknown as ReturnType<typeof hooks.useScheduledTask>);
}

function setRuns(
  runs: ScheduledTaskRun[],
  state: { isLoading?: boolean; isError?: boolean; cached?: boolean } = {},
) {
  vi.mocked(hooks.useScheduledTaskRuns).mockReturnValue({
    data: runs,
    isLoading: state.isLoading ?? false,
    isError: state.isError ?? false,
    // `cached`: data the query held from before this mount, not yet refetched.
    isFetchedAfterMount: !(state.cached ?? false),
  } as unknown as ReturnType<typeof hooks.useScheduledTaskRuns>);
}

beforeEach(() => {
  routeParams = { taskId: "st_1" };
  useAvailableAgents.mockImplementation(defaultAgentCatalog);
  navigate.mockReset();
  updateMutate.mockReset();
  deleteMutate.mockReset();
  runNowMutate.mockReset();
  showToast.mockReset();
  resetReadStateForTests();
  seedRunUnreadBaseline.mockReset();
  vi.mocked(hooks.useUpdateScheduledTask).mockReturnValue({
    mutate: updateMutate,
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useUpdateScheduledTask>);
  vi.mocked(hooks.useDeleteScheduledTask).mockReturnValue({
    mutate: deleteMutate,
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useDeleteScheduledTask>);
  vi.mocked(hooks.useRunScheduledTaskNow).mockReturnValue({
    mutate: runNowMutate,
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useRunScheduledTaskNow>);
  setRuns([]);
});

afterEach(cleanup);

function pageTree() {
  // QueryClient required by other hooks in the tree (useScheduledTask, etc.).
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <MemoryRouter>
          <TaskDetailPage />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

function renderPage() {
  return render(pageTree());
}

describe("header, prompt, configuration", () => {
  it("renders the title, prompt, schedule, and agent/host config", () => {
    setTask(task());
    renderPage();
    expect(screen.getByTestId("task-detail-title")).toHaveTextContent("Nightly triage");
    expect(screen.getByTestId("task-detail-prompt")).toHaveTextContent(
      "Triage the inbox and summarize.",
    );
    expect(screen.getByTestId("task-detail-schedule").textContent).toContain("Weekdays at 8:00 AM");
    expect(screen.getByTestId("task-detail-agent")).toHaveTextContent("Polly");
    // Null hostId → resolve-at-fire label, not blank.
    expect(screen.getByTestId("task-detail-host")).toHaveTextContent("Most recently active host");
    expect(screen.getByTestId("task-detail-state-pill")).toHaveTextContent("Active");
  });

  it("keeps the task's agent in the catalog when another agent shares its name", () => {
    // The catalog merges agents by name and keeps a pinned id; without the pin
    // a session's same-named copy wins and the task's own agent drops out.
    useAvailableAgents.mockImplementation((options) => ({
      data: options?.pinnedAgentIds?.includes("ag_1")
        ? [{ id: "ag_1", name: "polly", display_name: "Polly" }]
        : [{ id: "ag_session_copy", name: "polly", display_name: "Polly (session copy)" }],
    }));
    setTask(task({ agentId: "ag_1" }));
    renderPage();
    expect(screen.getByTestId("task-detail-agent")).toHaveTextContent(/^Polly$/);
  });

  it("names the sandbox for a task that runs in a new sandbox each time", () => {
    setTask(task({ executionTarget: "managed_sandbox" }));
    renderPage();
    expect(screen.getByTestId("task-detail-host")).toHaveTextContent("New Sandbox");
  });

  it("resolves a pinned host id to its name", () => {
    setTask(task({ hostId: "h_1" }));
    renderPage();
    expect(screen.getByTestId("task-detail-host")).toHaveTextContent("My Laptop");
  });

  it("shows a Paused pill and a resume affordance for a paused task", () => {
    setTask(task({ state: "paused" }));
    renderPage();
    expect(screen.getByTestId("task-detail-state-pill")).toHaveTextContent("Paused");
    const toggle = screen.getByTestId("task-detail-pause-toggle");
    // The toggle is a Switch (role="switch"), OFF (unchecked) when paused.
    expect(toggle).toHaveAttribute("role", "switch");
    expect(toggle).toHaveAttribute("aria-label", "Resume automation");
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  it("the active-state toggle Switch is ON for an active task", () => {
    setTask(task({ state: "active" }));
    renderPage();
    const toggle = screen.getByTestId("task-detail-pause-toggle");
    expect(toggle).toHaveAttribute("role", "switch");
    expect(toggle).toHaveAttribute("aria-label", "Pause automation");
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });
});

describe("loading / not-found states", () => {
  it("renders a spinner while loading", () => {
    setTask(undefined, { isLoading: true });
    renderPage();
    expect(screen.getByTestId("task-detail-loading")).toBeInTheDocument();
  });

  it("renders a friendly not-found with a back link when the task 404s", () => {
    setTask(undefined, { error: new ScheduledTaskApiError("not found", 404, "not_found") });
    renderPage();
    expect(screen.getByTestId("task-detail-not-found")).toBeInTheDocument();
    expect(screen.getByTestId("task-detail-back")).toBeInTheDocument();
  });

  it("offers a retry, not a not-found, when loading the task fails for another reason", () => {
    refetchTask.mockReset();
    setTask(undefined, { error: new ScheduledTaskApiError("unavailable", 503, null) });
    renderPage();
    expect(screen.queryByTestId("task-detail-not-found")).toBeNull();
    expect(screen.getByTestId("task-detail-load-error")).toHaveTextContent(
      "Couldn’t load this automation.",
    );
    fireEvent.click(screen.getByTestId("task-detail-retry"));
    expect(refetchTask).toHaveBeenCalledOnce();
  });
});

describe("header actions", () => {
  it("Run now shows 'Run started' toast on success", () => {
    runNowMutate.mockImplementation(
      (_id: string, opts?: { onSuccess?: () => void; onError?: () => void }) => opts?.onSuccess?.(),
    );
    setTask(task());
    renderPage();
    fireEvent.click(screen.getByTestId("task-detail-run-now"));
    expect(showToast).toHaveBeenCalledWith("Run started");
  });

  it("Run now shows error toast on a generic (non-409) failure", () => {
    runNowMutate.mockImplementation(
      (_id: string, opts?: { onSuccess?: () => void; onError?: (err: unknown) => void }) =>
        opts?.onError?.(new ScheduledTaskApiError("server error", 503, null)),
    );
    setTask(task());
    renderPage();
    fireEvent.click(screen.getByTestId("task-detail-run-now"));
    expect(showToast).toHaveBeenCalledWith("Couldn't start the run");
  });

  it("Run now shows 'already in progress' toast for a 409 (overlap guard hit)", () => {
    runNowMutate.mockImplementation(
      (_id: string, opts?: { onSuccess?: () => void; onError?: (err: unknown) => void }) =>
        opts?.onError?.(new ScheduledTaskApiError("conflict", 409, "CONFLICT")),
    );
    setTask(task());
    renderPage();
    fireEvent.click(screen.getByTestId("task-detail-run-now"));
    expect(showToast).toHaveBeenCalledWith("This run is already in progress");
  });

  it("Run now button shows spinner + 'In progress' while pending and re-enables after", () => {
    // Simulate a pending mutation (isPending=true while it hasn't resolved).
    vi.mocked(hooks.useRunScheduledTaskNow).mockReturnValue({
      mutate: runNowMutate,
      isPending: true,
    } as unknown as ReturnType<typeof hooks.useRunScheduledTaskNow>);
    setTask(task());
    renderPage();
    const btn = screen.getByTestId("task-detail-run-now");
    // Button is disabled while pending (busy includes runNowMutation.isPending).
    expect(btn).toBeDisabled();
    // Shows "In progress" text (not "Run now").
    expect(btn).toHaveTextContent("In progress");
    expect(btn.querySelector(".animate-spin")).not.toBeNull();

    // Once the mutation resolves (isPending=false), button re-enables with "Run now".
    vi.mocked(hooks.useRunScheduledTaskNow).mockReturnValue({
      mutate: runNowMutate,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useRunScheduledTaskNow>);
    // Re-render by re-calling renderPage on a fresh DOM is awkward; just check the
    // idle state in a separate render.
    cleanup();
    renderPage();
    const idleBtn = screen.getByTestId("task-detail-run-now");
    expect(idleBtn).not.toBeDisabled();
    expect(idleBtn).toHaveTextContent("Run now");
    expect(idleBtn.querySelector(".animate-spin")).toBeNull();
  });

  it("Run now button stays 'In progress' after 202 until a new run row appears", async () => {
    // Start with empty runs so preFireNewestId = null.
    setTask(task());
    setRuns([]);
    runNowMutate.mockImplementation(
      (_id: string, opts?: { onSuccess?: () => void; onError?: (err: unknown) => void }) =>
        opts?.onSuccess?.(),
    );
    const { rerender } = renderPage();

    const btn = screen.getByTestId("task-detail-run-now");
    await act(async () => {
      fireEvent.click(btn);
    });

    // After onSuccess fires, button is in "In progress" state (awaitingRunRow=true).
    expect(btn).toBeDisabled();
    expect(btn).toHaveTextContent("In progress");

    // Simulate the accelerated poll delivering a new run row.
    setRuns([run({ id: "run_new", status: "running", conversationId: "c_new" })]);
    await act(async () => {
      rerender(
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}
        >
          <TooltipProvider>
            <MemoryRouter>
              <TaskDetailPage />
            </MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>,
      );
    });

    // New row appeared (id changed) → flag cleared → button re-enables.
    expect(btn).not.toBeDisabled();
    expect(btn).toHaveTextContent("Run now");
  });

  it("Run now button re-enables after 20s safety cap if no row arrives", async () => {
    vi.useFakeTimers();
    try {
      setTask(task());
      setRuns([]);
      runNowMutate.mockImplementation(
        (_id: string, opts?: { onSuccess?: () => void; onError?: (err: unknown) => void }) =>
          opts?.onSuccess?.(),
      );
      renderPage();
      const btn = screen.getByTestId("task-detail-run-now");
      await act(async () => {
        fireEvent.click(btn);
      });
      expect(btn).toBeDisabled();
      expect(btn).toHaveTextContent("In progress");

      // Advance past the 20s safety cap.
      await act(async () => {
        vi.advanceTimersByTime(20_001);
      });
      expect(btn).not.toBeDisabled();
      expect(btn).toHaveTextContent("Run now");
    } finally {
      vi.useRealTimers();
    }
  });

  it("toggling the Switch off pauses an active task (update to paused)", () => {
    setTask(task({ state: "active" }));
    renderPage();
    // Clicking the Switch fires onCheckedChange → handlePauseToggle.
    fireEvent.click(screen.getByTestId("task-detail-pause-toggle"));
    expect(updateMutate).toHaveBeenCalledWith({ id: "st_1", input: { state: "paused" } });
  });

  it("toggling the Switch on resumes a paused task (update to active)", () => {
    setTask(task({ state: "paused" }));
    renderPage();
    fireEvent.click(screen.getByTestId("task-detail-pause-toggle"));
    expect(updateMutate).toHaveBeenCalledWith({ id: "st_1", input: { state: "active" } });
  });

  it("delete fires the mutation and navigates back to the list on success", () => {
    setTask(task());
    // Make the delete mutation invoke its onSuccess callback synchronously.
    deleteMutate.mockImplementation((_id: string, opts?: { onSuccess?: () => void }) =>
      opts?.onSuccess?.(),
    );
    renderPage();
    fireEvent.click(screen.getByTestId("task-detail-delete"));
    expect(deleteMutate).toHaveBeenCalledWith(
      "st_1",
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
    expect(navigate).toHaveBeenCalledWith("/tasks");
  });

  it("edit opens the reused create dialog in edit mode", () => {
    setTask(task());
    renderPage();
    expect(screen.queryByTestId("edit-dialog-open")).toBeNull();
    fireEvent.click(screen.getByTestId("task-detail-edit"));
    expect(screen.getByTestId("edit-dialog-open")).toHaveAttribute("data-editing-id", "st_1");
  });
});

describe("run history", () => {
  it("renders the empty state when there are no runs", () => {
    setTask(task());
    setRuns([]);
    renderPage();
    expect(screen.getByTestId("task-detail-runs-empty")).toHaveTextContent("No runs yet.");
  });

  it("renders a READ succeeded run with a grey dot ('Completed' tooltip) and a clickable row", async () => {
    // No read state for the conversation, and no server unread flag.
    setTask(task());
    setRuns([run({ status: "succeeded", conversationId: "c_9" })]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    expect(row).toHaveAttribute("data-run-status", "succeeded");
    expect(within(row).getByTestId("run-duration")).toHaveTextContent("1m 42s");
    // A success has nothing to explain below the timestamp.
    expect(within(row).queryByTestId("run-failure")).toBeNull();
    // Read success → a muted grey dot (left column), not the blue unread one.
    await waitFor(() => expect(row).toHaveAttribute("data-run-unread", "false"));
    const dot = within(row).getByTestId("run-status-dot");
    expect(dot).toHaveAttribute("data-run-icon", "read");
    expect(dot.className).toContain("bg-muted-foreground/40");
    // The whole row is the anchor to /c/:conversationId, and it names the status
    // for assistive technology, which can't see the dot.
    const link = within(row).getByTestId("run-open");
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("href", "/c/c_9");
    expect(link.className).toContain("cursor-pointer");
    expect(link).toHaveAccessibleName(/^Completed/);
  });

  it("renders an unread dot (brand-accent) with an 'Unread' tooltip for an unread success", async () => {
    markConversationUnread("c_9", 1_700_000_500);
    setTask(task());
    setRuns([run({ status: "succeeded", conversationId: "c_9" })]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    // The dot flips to the unread state once the unread query resolves.
    await waitFor(() => expect(row).toHaveAttribute("data-run-unread", "true"));
    const dot = within(row).getByTestId("run-status-dot");
    expect(dot).toHaveAttribute("data-run-icon", "unread");
    // Color matches the sidebar's unread indicator (brand-accent, not blue-500).
    expect(dot.className).toContain("bg-brand-accent");
    expect(within(row).getByTestId("run-open")).toHaveAccessibleName(/^Unread/);
    // No status ICON (that leading slot only holds fail/skip icons).
    expect(within(row).queryByTestId("run-status-icon")).toBeNull();
    // Focusing the tooltip trigger (Radix opens on focus in jsdom) → "Unread".
    fireEvent.focus(dot);
    await waitFor(() => expect(screen.getAllByText("Unread").length).toBeGreaterThan(0));
  });

  it("shows the server's viewerUnread until the session is opened, then clears without a refetch", async () => {
    // No local read state yet, as on a fresh page load: the server flag drives the dot.
    setTask(task());
    setRuns([
      run({
        status: "succeeded",
        conversationId: "c_server_unread",
        conversationUpdatedAt: 1_700_000_500,
        conversationStatus: "idle",
        viewerUnread: true,
      }),
    ]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    await waitFor(() => expect(row).toHaveAttribute("data-run-unread", "true"));
    const dot = within(row).getByTestId("run-status-dot");
    expect(dot).toHaveAttribute("data-run-icon", "unread");

    // Opening the session writes local read state; the cached run payload still
    // says viewerUnread, but the newer local read wins.
    act(() => {
      seedReadState([]);
      markConversationSeen("c_server_unread", 1_700_000_600);
    });
    await waitFor(() => expect(row).toHaveAttribute("data-run-unread", "false"));
  });

  it("treats a deleted/missing conversation (no payload) as read: grey dot, still clickable", async () => {
    markConversationUnread("c_gone", 1_700_000_500); // would be unread IF it existed
    setTask(task());
    // A run whose conversation was deleted: the backend returns null for the
    // enriched fields (no conversation found in the store).
    setRuns([
      run({
        status: "succeeded",
        conversationId: "c_gone",
        conversationUpdatedAt: null,
        conversationStatus: null,
        viewerUnread: null,
      }),
    ]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    await waitFor(() => expect(row).toHaveAttribute("data-run-unread", "false"));
    const dot = within(row).getByTestId("run-status-dot");
    expect(dot).toHaveAttribute("data-run-icon", "read");
    expect(dot.className).toContain("bg-muted-foreground/40");
    expect(within(row).getByTestId("run-open")).toHaveAttribute("href", "/c/c_gone");
  });

  it("reads a session the sidebar hasn't loaded by its server last-seen", async () => {
    // Read on another device at 400, updated at 500, never flagged unread, and
    // unknown to this browser: unread, as the session list would show it.
    setTask(task());
    setRuns([
      run({
        id: "run_seen_before",
        conversationId: "c_remote",
        conversationUpdatedAt: 1_700_000_500,
        viewerLastSeen: 1_700_000_400,
        viewerUnread: false,
      }),
    ]);
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("task-detail-run")).toHaveAttribute("data-run-unread", "true"),
    );
  });

  it("reads a session seen after its last update as read", () => {
    setTask(task());
    setRuns([
      run({
        conversationId: "c_remote",
        conversationUpdatedAt: 1_700_000_500,
        viewerLastSeen: 1_700_000_500,
        viewerUnread: false,
      }),
    ]);
    renderPage();
    expect(screen.getByTestId("task-detail-run")).toHaveAttribute("data-run-unread", "false");
  });

  it("renders a FAILED run with an amber triangle and the stage and reason it stopped at", async () => {
    setTask(task());
    setRuns([
      run({
        id: "run_fail",
        status: "failed",
        errorCode: "sandbox_registration_timeout",
        conversationId: null,
      }),
    ]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    const icon = within(row).getByTestId("run-status-icon");
    expect(icon).toHaveAttribute("data-run-icon", "failed");
    expect(icon.getAttribute("class") ?? "").toContain("text-amber-500");
    expect(within(row).getByTestId("run-failure-stage")).toHaveTextContent("Sandbox");
    expect(within(row).getByTestId("run-failure")).toHaveTextContent(
      "Sandbox · The sandbox didn't connect in time.",
    );
    fireEvent.focus(icon);
    await waitFor(() => expect(screen.getAllByText("Failed").length).toBeGreaterThan(0));
  });

  it("names no stage for an unknown error code but still gives a reason", () => {
    setTask(task());
    setRuns([run({ status: "failed", errorCode: "some_new_code", conversationId: null })]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    expect(within(row).queryByTestId("run-failure-stage")).toBeNull();
    expect(within(row).getByTestId("run-failure")).toHaveTextContent("Run failed.");
  });

  it("keeps a failed run that created a session linked to it", () => {
    setTask(task());
    setRuns([run({ status: "failed", errorCode: "runner_start_failed", conversationId: "c_7" })]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    const link = within(row).getByTestId("run-open");
    expect(link).toHaveAttribute("href", "/c/c_7");
    expect(link).toHaveAccessibleName(/Runner · The agent didn't start\./);
    expect(within(row).getByTestId("run-failure")).toHaveTextContent(
      "Runner · The agent didn't start.",
    );
  });

  it("labels a run that hasn't started yet as Queued", () => {
    setTask(task());
    setRuns([run({ status: "scheduled", firedAt: null, finishedAt: null, conversationId: null })]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    expect(within(row).getByTestId("run-status-label")).toHaveTextContent("Queued");
    expect(within(row).getByTestId("run-status-dot")).toHaveAttribute("data-run-icon", "pending");
  });

  it("shows a starting run with a spinner", async () => {
    setTask(task());
    setRuns([run({ status: "starting", firedAt: null, finishedAt: null, conversationId: null })]);
    renderPage();
    const icon = within(screen.getByTestId("task-detail-run")).getByTestId("run-status-icon");
    expect(icon).toHaveAttribute("data-run-icon", "starting");
    expect(
      within(screen.getByTestId("task-detail-run")).getByTestId("run-status-label"),
    ).toHaveTextContent("Starting");
    fireEvent.focus(icon);
    await waitFor(() => expect(screen.getAllByText("Starting").length).toBeGreaterThan(0));
  });

  it("renders a skipped run with a muted calendar-off icon and is NOT clickable", async () => {
    setTask(task());
    setRuns([
      run({
        id: "run_skip",
        status: "skipped",
        errorCode: "no_online_host",
        firedAt: null,
        finishedAt: null,
        conversationId: null,
      }),
    ]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    const icon = within(row).getByTestId("run-status-icon");
    expect(icon).toHaveAttribute("data-run-icon", "skipped");
    expect(icon.getAttribute("class") ?? "").toContain("text-muted-foreground");
    expect(within(row).getByTestId("run-failure")).toHaveTextContent(
      "Host · None of your hosts was online.",
    );
    fireEvent.focus(icon);
    await waitFor(() => expect(screen.getAllByText("Skipped").length).toBeGreaterThan(0));
    // No conversation → NOT a clickable row (no anchor, no hover/pointer), and
    // no finished timestamps → no duration.
    expect(within(row).queryByTestId("run-open")).toBeNull();
    expect(within(row).queryByTestId("run-duration")).toBeNull();
  });

  it("renders a RUNNING run with a spinning loader icon and a 'Running' tooltip", async () => {
    setTask(task());
    setRuns([
      run({
        id: "run_running",
        status: "running",
        conversationId: null,
        firedAt: 1_700_000_000,
        finishedAt: null,
      }),
    ]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    const icon = within(row).getByTestId("run-status-icon");
    expect(icon).toHaveAttribute("data-run-icon", "running");
    expect(icon.getAttribute("class") ?? "").toContain("animate-spin");
    // It is an svg icon, not the dot span.
    expect(within(row).queryByTestId("run-status-dot")).toBeNull();
    // Tooltip reads "Running".
    fireEvent.focus(icon);
    await waitFor(() => expect(screen.getAllByText("Running").length).toBeGreaterThan(0));
  });

  it("a RUNNING run with a conversationId renders a clickable link", () => {
    setTask(task());
    setRuns([
      run({
        id: "run_running_linked",
        status: "running",
        conversationId: "c_run",
        firedAt: 1_700_000_000,
        finishedAt: null,
      }),
    ]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    const link = within(row).getByTestId("run-open");
    expect(link).toHaveAttribute("href", "/c/c_run");
    expect(link.tagName).toBe("A");
  });

  it("renders an error message when run history fails to load", () => {
    setTask(task());
    setRuns([], { isError: true });
    renderPage();
    expect(screen.getByTestId("task-detail-runs-error")).toBeInTheDocument();
  });
});

describe("auto-mark runs unread", () => {
  it("marks a newly-completed run unread when it transitions running→succeeded", async () => {
    setTask(task());
    // First render: one running run (sets baseline — NOT marked).
    setRuns([
      run({
        id: "run_new",
        status: "running",
        conversationId: "c_new",
        firedAt: 1_700_000_000,
        finishedAt: null,
        conversationUpdatedAt: 1_700_000_100,
        conversationStatus: "running",
      }),
    ]);
    const { rerender } = renderPage();
    expect(seedRunUnreadBaseline).not.toHaveBeenCalled();

    // The run completes a second after its session's last update: completion
    // and the session's updated_at are written separately.
    setRuns([
      run({
        id: "run_new",
        status: "succeeded",
        conversationId: "c_new",
        firedAt: 1_700_000_000,
        finishedAt: 1_700_000_101,
        conversationUpdatedAt: 1_700_000_100,
        conversationStatus: "idle",
      }),
    ]);
    rerender(pageTree());

    expect(seedRunUnreadBaseline).toHaveBeenCalledWith("c_new", 1_700_000_100);
    await waitFor(() =>
      expect(screen.getByTestId("task-detail-run")).toHaveAttribute("data-run-unread", "true"),
    );
  });

  it("keeps a session read when it was read at or after the completed run's last update", async () => {
    setTask(task());
    setRuns([]);
    const { rerender } = renderPage();
    act(() => {
      seedReadState([]);
      markConversationSeen("c_read", 1_700_000_100);
    });

    setRuns([
      run({
        status: "succeeded",
        conversationId: "c_read",
        conversationUpdatedAt: 1_700_000_100,
        conversationStatus: "idle",
      }),
    ]);
    rerender(pageTree());

    await waitFor(() =>
      expect(screen.getByTestId("task-detail-run")).toHaveAttribute("data-run-unread", "false"),
    );
  });

  it("does NOT re-mark a run that was already opened (resurrection guard)", () => {
    setTask(task());
    // First render: empty runs (baseline seeded as empty).
    setRuns([]);
    const { rerender } = renderPage();
    expect(seedRunUnreadBaseline).not.toHaveBeenCalled();

    // Run completes — gets marked once.
    setRuns([run({ id: "run_1", status: "succeeded", conversationId: "c_opened" })]);
    rerender(pageTree());
    expect(seedRunUnreadBaseline).toHaveBeenCalledTimes(1);

    // The user opens the session, then the next poll returns the same runs.
    act(() => {
      seedReadState([]);
      markConversationSeen("c_opened", 1_800_000_000);
    });
    rerender(pageTree());
    // The id is in the ref, so the open isn't undone.
    expect(seedRunUnreadBaseline).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("task-detail-run")).toHaveAttribute("data-run-unread", "false");
  });

  it("does NOT auto-mark pre-existing terminal runs on first page load (no-retroactive guard)", () => {
    setTask(task());
    // First render: two already-succeeded runs (pre-existing history).
    setRuns([
      run({ id: "run_old_1", status: "succeeded", conversationId: "c_old_1" }),
      run({ id: "run_old_2", status: "succeeded", conversationId: "c_old_2" }),
    ]);
    renderPage();
    // Baseline seeds both ids as already-seen → seedRunUnreadBaseline never called.
    expect(seedRunUnreadBaseline).not.toHaveBeenCalled();
  });

  it("does NOT auto-mark a run that finished while the page was closed (stale cache)", () => {
    setTask(task());
    // Returning to the page: the query still holds the run as running from the
    // last visit, and the first fetch after mounting shows it already done.
    setRuns(
      [run({ id: "run_away", status: "running", conversationId: "c_away", finishedAt: null })],
      { cached: true },
    );
    const { rerender } = renderPage();
    setRuns([run({ id: "run_away", status: "succeeded", conversationId: "c_away" })]);
    rerender(pageTree());

    expect(seedRunUnreadBaseline).not.toHaveBeenCalled();
    expect(screen.getByTestId("task-detail-run")).toHaveAttribute("data-run-unread", "false");
  });

  it("does NOT auto-mark another task's history when the route switches tasks in place", () => {
    setTask(task());
    setRuns([run({ id: "run_a", status: "succeeded", conversationId: "c_a" })]);
    const { rerender } = renderPage();

    // Browser history jumps straight to task B, keeping this route mounted: its
    // cached history first, then its first fetched response.
    routeParams = { taskId: "st_2" };
    setTask(task({ id: "st_2" }));
    setRuns([run({ id: "run_b", status: "succeeded", conversationId: "c_b" })], { cached: true });
    rerender(pageTree());
    setRuns([run({ id: "run_b", status: "succeeded", conversationId: "c_b" })]);
    rerender(pageTree());

    expect(seedRunUnreadBaseline).not.toHaveBeenCalled();
  });
});
