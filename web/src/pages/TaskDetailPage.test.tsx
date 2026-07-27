// Tests for the scheduled-task DETAIL page (`/tasks/:taskId`): header + prompt
// + configuration + run-history rendering from mocked hooks, the header actions
// (Run now fires the mutation, delete navigates back, edit opens the dialog),
// and the run-row rendering rules (status dots, duration, errorCode messages,
// and the "Open conversation" link — never a fabricated summary).
//
// All data hooks are mocked at their seam; the edit dialog is stubbed so we
// only assert it opened. useParams/useNavigate come from `@/lib/routing`.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetailPage } from "./TaskDetailPage";
import * as hooks from "@/hooks/useScheduledTasks";
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
}));

// Agent / host catalogs — return simple lists so labels resolve.
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [{ id: "ag_1", name: "polly", display_name: "Polly" }],
  }),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [{ host_id: "h_1", name: "My Laptop" }] }),
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
  return render(
    <MemoryRouter>
      <TaskDetailPage />
    </MemoryRouter>,
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

  it("renders the status line as [state pill] [toggle] [schedule], in that order", () => {
    setTask(task({ state: "active" }));
    renderPage();
    const pill = screen.getByTestId("task-detail-state-pill");
    const toggle = screen.getByTestId("task-detail-pause-toggle");
    const schedule = screen.getByTestId("task-detail-schedule");
    // Document order: pill before toggle before schedule.
    expect(pill.compareDocumentPosition(toggle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      toggle.compareDocumentPosition(schedule) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
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
  it("Run now fires the run mutation", () => {
    setTask(task());
    renderPage();
    fireEvent.click(screen.getByTestId("task-detail-run-now"));
    expect(runNowMutate).toHaveBeenCalledWith("st_1");
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

  it("renders a succeeded run with a duration and an Open conversation link", () => {
    setTask(task());
    setRuns([run({ status: "succeeded", conversationId: "c_9" })]);
    renderPage();
    const row = screen.getByTestId("task-detail-run");
    expect(row).toHaveAttribute("data-run-status", "succeeded");
    expect(within(row).getByTestId("run-duration")).toHaveTextContent("1m 42s");
    const link = within(row).getByTestId("run-open-conversation");
    expect(link).toHaveAttribute("href", "/c/c_9");
    // Never a fabricated summary line.
    expect(within(row).queryByTestId("run-error-detail")).toBeNull();
  });

  it("renders a skipped run's errorCode-derived message, not a conversation link", () => {
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
    expect(within(row).getByTestId("run-error-detail")).toHaveTextContent(
      "Host was offline at fire time — run skipped",
    );
    expect(within(row).queryByTestId("run-open-conversation")).toBeNull();
    // No finished timestamps → no duration shown.
    expect(within(row).queryByTestId("run-duration")).toBeNull();
  });

  it("renders an error message when run history fails to load", () => {
    setTask(task());
    setRuns([], { isError: true });
    renderPage();
    expect(screen.getByTestId("task-detail-runs-error")).toBeInTheDocument();
  });
});
