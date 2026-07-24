// Tests for ScheduledTaskRow: the last-run completion pill (one per status,
// plus the no-pill cases), the server-sourced next-run text, and the ⋯ menu
// actions (Run now / Pause-Resume / Edit / Delete) dispatching their callbacks.
//
// The row is fully props-driven, so these render it directly with a built task
// object — no hook mocking needed. The ⋯ menu is a Radix dropdown; opening it
// uses pointerDown on the trigger (matching the TasksPage tests).

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ScheduledTaskRow } from "./ScheduledTaskRow";
import type { ScheduledTask, ScheduledTaskRunStatus } from "@/lib/scheduledTasksApi";

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

function renderRow(
  t: ScheduledTask,
  handlers: Partial<Parameters<typeof ScheduledTaskRow>[0]> = {},
) {
  const props = {
    task: t,
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

describe("last-run status pill", () => {
  it.each<[ScheduledTaskRunStatus, string]>([
    ["failed", "Failed"],
    ["incomplete", "Failed"],
    ["skipped", "Skipped"],
    ["running", "Running"],
    ["scheduled", "Queued"],
  ])("renders the %s status as a '%s' pill", (status, label) => {
    renderRow(task({ lastRunStatus: status }));
    const pill = screen.getByTestId("task-run-status-pill");
    expect(pill).toHaveTextContent(label);
    expect(pill).toHaveAttribute("data-status", status);
  });

  it("renders NO status pill when the task has never run (null status)", () => {
    renderRow(task({ lastRunStatus: null }));
    expect(screen.queryByTestId("task-run-status-pill")).toBeNull();
  });

  it("renders NO status pill for a succeeded run (success is not noise)", () => {
    renderRow(task({ lastRunStatus: "succeeded" }));
    expect(screen.queryByTestId("task-run-status-pill")).toBeNull();
  });

  it("marks failed distinctly from skipped (destructive vs muted)", () => {
    const { rerender } = render(
      <ScheduledTaskRow
        task={task({ lastRunStatus: "failed" })}
        onEdit={vi.fn()}
        onPauseToggle={vi.fn()}
        onRunNow={vi.fn()}
        onDelete={vi.fn()}
        busy={false}
      />,
    );
    expect(screen.getByTestId("task-run-status-pill").className).toContain("text-destructive");
    rerender(
      <ScheduledTaskRow
        task={task({ lastRunStatus: "skipped" })}
        onEdit={vi.fn()}
        onPauseToggle={vi.fn()}
        onRunNow={vi.fn()}
        onDelete={vi.fn()}
        busy={false}
      />,
    );
    const skipped = screen.getByTestId("task-run-status-pill");
    expect(skipped.className).toContain("text-muted-foreground");
    expect(skipped.className).not.toContain("text-destructive");
  });
});

describe("next-run text (server-sourced)", () => {
  it("renders the formatted next-run when nextRunAt is set", () => {
    // A fixed future instant so the formatter produces stable text.
    renderRow(task({ nextRunAt: "2999-01-02T09:00:00Z", timezone: "UTC" }));
    const nextRun = screen.getByTestId("task-next-run");
    expect(nextRun.textContent).toMatch(/Next:/);
    // The date/time formatting is exercised in scheduleText.test.ts; here we
    // just assert the row surfaces the server value.
    expect(nextRun.textContent).toMatch(/9:00 AM|9:00 AM/);
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
