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
import { ScheduledTaskApiError } from "@/lib/scheduledTasksApi";
import type { ScheduledTask, ScheduledTaskRun } from "@/lib/scheduledTasksApi";

const navigate = vi.fn();
vi.mock("@/lib/routing", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/routing")>();
  return {
    ...actual,
    useNavigate: () => navigate,
    useParams: () => ({ taskId: "st_1" }),
  };
});

vi.mock("@/hooks/useScheduledTasks", () => ({
  useScheduledTask: vi.fn(),
  useScheduledTaskRuns: vi.fn(),
  useUpdateScheduledTask: vi.fn(),
  useDeleteScheduledTask: vi.fn(),
  useRunScheduledTaskNow: vi.fn(),
  cancelRunNowPoll: vi.fn(),
}));

const showToast = vi.fn();
vi.mock("@/components/ui/toast", () => ({ showToast: (...args: unknown[]) => showToast(...args) }));

// Agent / host catalogs — return simple lists so labels resolve.
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [{ id: "ag_1", name: "polly", display_name: "Polly" }],
  }),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [{ host_id: "h_1", name: "My Laptop" }] }),
}));

// Read-state mirror: control unread verdicts deterministically. Default read.
const isConversationUnseen = vi.fn(() => false);
const isExplicitlyUnread = vi.fn(() => false);
const seedRunUnreadBaseline = vi.fn();
vi.mock("@/hooks/useUnseenConversations", () => ({
  useUnseenTick: () => 0,
  isConversationUnseen: (...args: unknown[]) => isConversationUnseen(...(args as [])),
  isExplicitlyUnread: (...args: unknown[]) => isExplicitlyUnread(...(args as [])),
  seedRunUnreadBaseline: (...args: unknown[]) => seedRunUnreadBaseline(...args),
}));

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
    workspace: null,
    hostId: null,
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
    viewerUnread: null,
    ...overrides,
  };
}

const updateMutate = vi.fn();
const deleteMutate = vi.fn();
const runNowMutate = vi.fn();

function setTask(
  data: ScheduledTask | undefined,
  state: { isLoading?: boolean; isError?: boolean } = {},
) {
  vi.mocked(hooks.useScheduledTask).mockReturnValue({
    data,
    isLoading: state.isLoading ?? false,
    isError: state.isError ?? false,
  } as unknown as ReturnType<typeof hooks.useScheduledTask>);
}

function setRuns(runs: ScheduledTaskRun[], state: { isLoading?: boolean; isError?: boolean } = {}) {
  vi.mocked(hooks.useScheduledTaskRuns).mockReturnValue({
    data: runs,
    isLoading: state.isLoading ?? false,
    isError: state.isError ?? false,
  } as unknown as ReturnType<typeof hooks.useScheduledTaskRuns>);
}

beforeEach(() => {
  navigate.mockReset();
  updateMutate.mockReset();
  deleteMutate.mockReset();
  runNowMutate.mockReset();
  showToast.mockReset();
  isConversationUnseen.mockReset();
  isConversationUnseen.mockReturnValue(false);
  isExplicitlyUnread.mockReset();
  isExplicitlyUnread.mockReturnValue(false);
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

function renderPage() {
  // QueryClient required by other hooks in the tree (useScheduledTask, etc.).
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <MemoryRouter>
          <TaskDetailPage />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
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
    expect(screen.getByTestId("task-detail-host")).toHaveTextContent("Auto (connected host)");
    expect(screen.getByTestId("task-detail-state-pill")).toHaveTextContent("Active");
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

  it("renders the status line as [toggle] [state pill] [schedule], in that order", () => {
    setTask(task({ state: "active" }));
    renderPage();
    const pill = screen.getByTestId("task-detail-state-pill");
    const toggle = screen.getByTestId("task-detail-pause-toggle");
    const schedule = screen.getByTestId("task-detail-schedule");
    // Document order: toggle before pill before schedule.
    expect(toggle.compareDocumentPosition(pill) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(pill.compareDocumentPosition(schedule) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("loading / not-found states", () => {
  it("renders a spinner while loading", () => {
    setTask(undefined, { isLoading: true });
    renderPage();
    expect(screen.getByTestId("task-detail-loading")).toBeInTheDocument();
  });

  it("renders a friendly not-found with a back link on error", () => {
    setTask(undefined, { isError: true });
    renderPage();
    expect(screen.getByTestId("task-detail-not-found")).toBeInTheDocument();
    expect(screen.getByTestId("task-detail-back")).toBeInTheDocument();
  });
});

describe("header actions", () => {
  it("Run now fires the run mutation with toast callbacks", () => {
    setTask(task());
    renderPage();
    fireEvent.click(screen.getByTestId("task-detail-run-now"));
    expect(runNowMutate).toHaveBeenCalledWith(
      "st_1",
      expect.objectContaining({
        onSuccess: expect.any(Function),
        onError: expect.any(Function),
      }),
    );
  });

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
    // Default mocks resolve read (isConversationUnseen → false).
    setTask(task());
    setRuns([run({ status: "succeeded", conversationId: "c_9" })]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    expect(row).toHaveAttribute("data-run-status", "succeeded");
    expect(within(row).getByTestId("run-duration")).toHaveTextContent("1m 42s");
    // No "Open conversation" link text — the whole row is the affordance now.
    expect(within(row).queryByTestId("run-open-conversation")).toBeNull();
    // No detail line below the timestamp (removed in FIX 1).
    expect(within(row).queryByTestId("run-error-detail")).toBeNull();
    // Read success → a muted grey dot (left column), not the blue unread one.
    await waitFor(() => expect(row).toHaveAttribute("data-run-unread", "false"));
    const dot = within(row).getByTestId("run-status-dot");
    expect(dot).toHaveAttribute("data-run-icon", "read");
    expect(dot.className).toContain("bg-muted-foreground/40");
    // Tooltip reads "Completed".
    fireEvent.focus(dot);
    await waitFor(() => expect(screen.getAllByText("Completed").length).toBeGreaterThan(0));
  });

  it("makes a succeeded run's whole row a link to its conversation", () => {
    setTask(task());
    setRuns([run({ status: "succeeded", conversationId: "c_9" })]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    const link = within(row).getByTestId("run-open");
    // The row itself is the anchor to /c/:conversationId (keyboard-focusable,
    // Enter/Space activate natively).
    expect(link).toHaveAttribute("href", "/c/c_9");
    expect(link.tagName).toBe("A");
    expect(link.className).toContain("cursor-pointer");
    expect(link.className).toContain("hover:bg-muted");
  });

  it("renders an unread dot (brand-accent) with an 'Unread' tooltip for an unread success", async () => {
    isConversationUnseen.mockReturnValue(true);
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
    // No status ICON (that leading slot only holds fail/skip icons).
    expect(within(row).queryByTestId("run-status-icon")).toBeNull();
    // Focusing the tooltip trigger (Radix opens on focus in jsdom) → "Unread".
    fireEvent.focus(dot);
    await waitFor(() => expect(screen.getAllByText("Unread").length).toBeGreaterThan(0));
  });

  it("shows unread dot from server viewerUnread=true with no per-row fetchConversationById call", async () => {
    // isConversationUnseen stays false (local mirror has no baseline yet, as on
    // a fresh page load). The server-side viewerUnread flag drives the dot.
    isConversationUnseen.mockReturnValue(false);
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
  });

  it("treats a deleted/missing conversation (no payload) as read: grey dot, still clickable", async () => {
    isConversationUnseen.mockReturnValue(true); // would be unread IF it existed
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
  });

  it("renders a FAILED run with an amber warning-triangle icon whose tooltip carries the error", async () => {
    setTask(task());
    setRuns([
      run({
        id: "run_fail",
        status: "failed",
        errorCode: "agent_error",
        conversationId: null,
      }),
    ]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    const icon = within(row).getByTestId("run-status-icon");
    expect(icon).toHaveAttribute("data-run-icon", "failed");
    expect(icon.getAttribute("class") ?? "").toContain("text-amber-500");
    // No inline detail line — the error message lives only in the icon tooltip.
    expect(within(row).queryByTestId("run-error-detail")).toBeNull();
    // The errorCode message is the icon's tooltip (Radix opens on focus).
    fireEvent.focus(icon);
    await waitFor(() => expect(screen.getAllByText("Run failed").length).toBeGreaterThan(0));
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
    // No inline detail line — skip reason lives only in the icon tooltip.
    expect(within(row).queryByTestId("run-error-detail")).toBeNull();
    // The skip reason is the icon's tooltip (Radix opens on focus).
    fireEvent.focus(icon);
    await waitFor(() =>
      expect(
        screen.getAllByText("Host was offline at fire time. Run skipped.").length,
      ).toBeGreaterThan(0),
    );
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
  it("marks a newly-completed run unread when it transitions running→succeeded", () => {
    setTask(task());
    // First render: one running run (sets baseline — NOT marked).
    setRuns([
      run({
        id: "run_new",
        status: "running",
        conversationId: "c_new",
        firedAt: 1_700_000_000,
        finishedAt: null,
      }),
    ]);
    const { rerender } = renderPage();
    // Baseline seeded with the running run (not terminal → not in autoMarked set).
    // seedRunUnreadBaseline should NOT have been called yet.
    expect(seedRunUnreadBaseline).not.toHaveBeenCalled();

    // Simulate the run completing: update the mock to return succeeded.
    setRuns([
      run({
        id: "run_new",
        status: "succeeded",
        conversationId: "c_new",
        firedAt: 1_700_000_000,
        finishedAt: 1_700_000_042,
      }),
    ]);
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

    expect(seedRunUnreadBaseline).toHaveBeenCalledWith("c_new", 1_700_000_042);
  });

  it("does NOT re-mark a run that was already opened (resurrection guard)", () => {
    setTask(task());
    // First render: empty runs (baseline seeded as empty).
    setRuns([]);
    const { rerender } = renderPage();
    expect(seedRunUnreadBaseline).not.toHaveBeenCalled();

    // Run completes — gets marked once.
    setRuns([run({ id: "run_1", status: "succeeded", conversationId: "c_opened" })]);
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
    expect(seedRunUnreadBaseline).toHaveBeenCalledTimes(1);

    // Simulate user opening the conversation (would call clearUnreadOverride /
    // markConversationSeen externally). Now poll re-fetches the same runs list.
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
    // seedRunUnreadBaseline must NOT be called again — the id is in the ref.
    expect(seedRunUnreadBaseline).toHaveBeenCalledTimes(1);
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

  it("uses seedRunUnreadBaseline (not markConversationUnread) so the dot clears when the conversation is opened", () => {
    // Regression: previously markConversationUnread set the explicitlyUnread
    // override, which blocked markConversationSeen in ChatPage (the initial-mount
    // guard skips clearUnreadOverride on a fresh mount). The dot would therefore
    // stick after the user clicked through. seedRunUnreadBaseline omits the
    // override so markConversationSeen isn't blocked.
    //
    // Verify: auto-mark calls seedRunUnreadBaseline (no override), NOT
    // markConversationUnread. The absence of markConversationUnread in the mock
    // confirms the dot is driven by the baseline alone — openable via
    // markConversationSeen without any guard blocking it.
    setTask(task());
    setRuns([]);
    const { rerender } = renderPage();

    setRuns([run({ id: "run_1", status: "succeeded", conversationId: "c_run" })]);
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

    // seedRunUnreadBaseline was called — not markConversationUnread.
    expect(seedRunUnreadBaseline).toHaveBeenCalledWith("c_run", expect.any(Number));
    // seedRunUnreadBaseline does NOT set the explicitlyUnread override, so
    // markConversationSeen is never blocked by it when the user clicks through.
  });
});
