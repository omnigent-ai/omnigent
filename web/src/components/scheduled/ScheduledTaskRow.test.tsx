// Tests for ScheduledTaskRow: the server-sourced next-run text and the ⋯ menu
// actions (Run now / Pause-Resume / Edit / Delete) dispatching their callbacks.
//
// The row is fully props-driven, so these render it directly with a built task
// object — no hook mocking needed. The ⋯ menu is a Radix dropdown; opening it
// uses pointerDown on the trigger (matching the TasksPage tests).

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ScheduledTaskRow } from "./ScheduledTaskRow";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";

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
  render(<ScheduledTaskRow {...props} />);
  return props;
}

afterEach(cleanup);

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
