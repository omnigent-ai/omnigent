// Tests for ScheduledTaskRow: the server-sourced next-run text and the ⋯ menu
// actions (Run now / Pause-Resume / Edit / Delete) dispatching their callbacks.
//
// The row is fully props-driven, so these render it directly with a built task
// object — no hook mocking needed. The ⋯ menu is a Radix dropdown; opening it
// uses pointerDown on the trigger (matching the TasksPage tests).

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ScheduledTaskRow } from "./ScheduledTaskRow";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type * as RoutingModule from "@/lib/routing";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";

// Capture navigate() calls; the row uses `@/lib/routing`'s useNavigate, which
// falls back to react-router-dom. Mocking it lets us assert the target without
// a full router history.
const navigate = vi.fn();
vi.mock("@/lib/routing", async (importOriginal) => {
  const actual = await importOriginal<typeof RoutingModule>();
  return { ...actual, useNavigate: () => navigate };
});

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

// A fixed `now` so the relative next-run label is deterministic (the real hook
// supplies a ticking clock; the row just formats the delta against whatever it's
// given).
const NOW = new Date("2026-03-10T12:00:00Z");

function renderRow(
  t: ScheduledTask,
  handlers: Partial<Parameters<typeof ScheduledTaskRow>[0]> = {},
  { detailPage = false }: { detailPage?: boolean } = {},
) {
  const props = {
    task: t,
    now: NOW,
    onEdit: vi.fn(),
    onPauseToggle: vi.fn(),
    onRunNow: vi.fn(),
    onDelete: vi.fn(),
    busy: false,
    ...handlers,
  };
  render(
    <CapabilitiesProvider
      info={{
        ...FALLBACK_SERVER_INFO,
        features: { automations_v2: detailPage },
      }}
    >
      <MemoryRouter>
        <ScheduledTaskRow {...props} />
      </MemoryRouter>
    </CapabilitiesProvider>,
  );
  return props;
}

afterEach(() => {
  cleanup();
  navigate.mockReset();
});

describe("next-run text (server-sourced, relative delta)", () => {
  it("renders the relative next-run with a 'Next run' prefix when nextRunAt is set", () => {
    // A near-future instant (2h1m past the fixed NOW) → a relative "in 2 hours"
    // label. Relative formatting is covered in scheduleText.test.ts; here we
    // assert the row surfaces it with the prefix, using the injected `now`.
    const future = new Date(NOW.getTime() + 2 * 60 * 60 * 1000 + 60_000).toISOString();
    renderRow(task({ nextRunAt: future }));
    const nextRun = screen.getByTestId("task-next-run");
    expect(nextRun.textContent).toBe("Next run in 2 hours");
  });

  it("renders NO next-run text when nextRunAt is null (paused / unarmed)", () => {
    renderRow(task({ nextRunAt: null }));
    expect(screen.queryByTestId("task-next-run")).toBeNull();
  });
});

describe("Project chip", () => {
  it("renders the resolved project's name and emoji", () => {
    renderRow(task({ projectId: "p_a" }), { project: { name: "Project A", icon: "📊" } });
    const chip = screen.getByTestId("task-project-chip");
    expect(chip).toHaveTextContent("Project A");
    expect(within(chip).getByTestId("project-icon")).toHaveTextContent("📊");
  });

  it("renders no chip without a resolved project", () => {
    renderRow(task({ projectId: "deleted" }));
    expect(screen.queryByTestId("task-project-chip")).toBeNull();
  });
});

describe("⋯ menu actions", () => {
  it("dispatches onRunNow when 'Run now' is selected", () => {
    const onRunNow = vi.fn();
    const t = task();
    renderRow(t, { onRunNow });
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    fireEvent.click(screen.getByTestId("task-run-now"));
    expect(onRunNow).toHaveBeenCalledWith(t);
  });

  it("offers Run now even for a paused task (manual override)", () => {
    const onRunNow = vi.fn();
    const t = task({ state: "paused" });
    renderRow(t, { onRunNow });
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    const runNow = screen.getByTestId("task-run-now");
    expect(runNow).toBeInTheDocument();
    fireEvent.click(runNow);
    expect(onRunNow).toHaveBeenCalledWith(t);
  });

  it("disables the menu trigger while the row is busy", () => {
    renderRow(task(), { busy: true });
    expect(screen.getByTestId("task-row-menu")).toBeDisabled();
  });
});

describe("card-body navigation", () => {
  it("navigates to the task's detail page when the card body is clicked", () => {
    const t = task({ id: "st_42" });
    renderRow(t, {}, { detailPage: true });
    fireEvent.click(screen.getByTestId("task-row-open"));
    expect(navigate).toHaveBeenCalledWith("/tasks/st_42");
  });

  it("keeps the card body inert while the detail page is behind its release flag", () => {
    renderRow(task({ id: "st_42" }));
    expect(screen.queryByTestId("task-row-open")).toBeNull();
    fireEvent.click(screen.getByText("Nightly triage"));
    expect(navigate).not.toHaveBeenCalled();
  });

  it("opening the ⋯ menu does NOT navigate", () => {
    renderRow(task({ id: "st_42" }), {}, { detailPage: true });
    // Radix opens on pointerdown; the trigger stops propagation so the
    // card-body navigation never fires.
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    expect(screen.getByTestId("task-run-now")).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("selecting a menu item dispatches its callback without navigating", () => {
    const onRunNow = vi.fn();
    const t = task({ id: "st_42" });
    renderRow(t, { onRunNow }, { detailPage: true });
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    fireEvent.click(screen.getByTestId("task-run-now"));
    expect(onRunNow).toHaveBeenCalledWith(t);
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe("last-run failure warning", () => {
  it.each(["active", "paused"] as const)("shows a red warning on a %s automation", (state) => {
    renderRow(task({ state, lastRunStatus: "failed" }), {}, { detailPage: true });
    const warning = screen.getByRole("img", { name: "Last run failed" });
    expect(warning).toHaveClass("text-status-red");
    expect(warning).toHaveAttribute("title", "Last run failed");
    expect(screen.getByTestId("task-row-open")).toHaveAccessibleDescription("Last run failed");
    expect(screen.getByTestId("task-row-open")).toHaveAttribute("aria-describedby", warning.id);
    fireEvent.click(warning);
    expect(navigate).toHaveBeenCalledWith("/tasks/st_1");
  });

  it.each([null, "succeeded", "running", "skipped"] as const)(
    "does not warn for %s",
    (lastRunStatus) => {
      renderRow(task({ lastRunStatus }));
      expect(screen.queryByRole("img", { name: "Last run failed" })).toBeNull();
    },
  );

  it("also warns when the detail page is disabled", () => {
    renderRow(task({ state: "paused", lastRunStatus: "failed" }));
    expect(screen.getByRole("img", { name: "Last run failed" })).toBeInTheDocument();
  });
});

it.each(["continue", "new"] as const)(
  "uses trigger controls only for continued templates: %s",
  async (sessionMode) => {
    renderRow(
      task({
        prompt: "This automation watches Slack and starts one Omnigent session",
        sessionMode,
        nextRunAt: "2026-03-10T14:00:00Z",
      }),
    );
    fireEvent.pointerDown(screen.getByTestId("task-row-menu"), { button: 0 });
    expect(await screen.findByTestId("task-edit")).toBeInTheDocument();
    if (sessionMode === "continue") {
      expect(screen.getByTestId("task-schedule-line")).toHaveTextContent(
        "Runs when new items match",
      );
      expect(screen.queryByTestId("task-next-run")).not.toBeInTheDocument();
      expect(screen.queryByTestId("task-run-now")).not.toBeInTheDocument();
    } else {
      expect(screen.getByTestId("task-next-run")).toBeInTheDocument();
      expect(screen.getByTestId("task-run-now")).toBeInTheDocument();
    }
  },
);

describe("right-click context menu", () => {
  it("opens the same actions on right-click and dispatches the selection", () => {
    const onDelete = vi.fn();
    const t = task();
    renderRow(t, { onDelete });
    fireEvent.contextMenu(screen.getByTestId("scheduled-task-row"));
    expect(screen.getByTestId("task-run-now")).toBeInTheDocument();
    expect(screen.getByTestId("task-edit")).toBeInTheDocument();
    expect(screen.getByTestId("task-pause-toggle").textContent).toBe("Pause");
    fireEvent.click(screen.getByTestId("task-delete"));
    expect(onDelete).toHaveBeenCalledWith(t);
  });

  it("opens on the clickable card body without navigating", () => {
    renderRow(task({ id: "st_42" }), {}, { detailPage: true });
    fireEvent.contextMenu(screen.getByTestId("task-row-open"));
    expect(screen.getByTestId("task-edit")).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not open while the row is busy", () => {
    renderRow(task(), { busy: true });
    fireEvent.contextMenu(screen.getByTestId("scheduled-task-row"));
    expect(screen.queryByTestId("task-run-now")).toBeNull();
  });
});
