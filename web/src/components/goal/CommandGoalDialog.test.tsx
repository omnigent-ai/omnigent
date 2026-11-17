import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Goal } from "@/lib/goalApi";
import { CommandGoalDialog, formatCommandGoalUsage } from "./CommandGoalDialog";

const CLAUDE_GOAL: Goal = {
  objective: "All tests pass",
  status: "complete",
  statusDetail: "The test run shows 12 passed",
  iterations: 3,
  tokenBudget: null,
  tokensUsed: 1200,
  timeUsedSeconds: 125,
  createdAt: 1,
  updatedAt: 2,
};

afterEach(cleanup);

describe("CommandGoalDialog", () => {
  it("submits a trimmed completion condition", () => {
    const onStartGoal = vi.fn();
    const onOpenChange = vi.fn();
    render(
      <CommandGoalDialog
        open
        onOpenChange={onOpenChange}
        readOnly={false}
        onStartGoal={onStartGoal}
      />,
    );

    fireEvent.change(screen.getByTestId("goal-condition"), {
      target: { value: "  All tests pass  " },
    });
    fireEvent.click(screen.getByTestId("goal-start"));

    expect(onStartGoal).toHaveBeenCalledWith("All tests pass");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("describes the selected goal backend", () => {
    render(
      <CommandGoalDialog
        open
        onOpenChange={vi.fn()}
        readOnly={false}
        onStartGoal={vi.fn()}
        backendLabel="Codex"
      />,
    );

    expect(screen.getByText(/Codex keeps working until this condition is met/)).toBeInTheDocument();
  });

  it("gives the scroll region room for the field's focus ring", () => {
    render(
      <CommandGoalDialog open onOpenChange={vi.fn()} readOnly={false} onStartGoal={vi.fn()} />,
    );

    const scrollRegion = screen.getByTestId("goal-condition").closest(".overflow-y-auto");
    if (!scrollRegion) throw new Error("goal scroll region not found");
    expect(scrollRegion).toHaveClass("p-1", "-m-1");
  });

  it("rejects an empty condition", () => {
    const onStartGoal = vi.fn();
    render(
      <CommandGoalDialog open onOpenChange={vi.fn()} readOnly={false} onStartGoal={onStartGoal} />,
    );

    fireEvent.click(screen.getByTestId("goal-start"));

    expect(screen.getByText("Goal condition cannot be empty.")).toBeInTheDocument();
    expect(onStartGoal).not.toHaveBeenCalled();
  });

  it("disables editing in read-only sessions", () => {
    render(<CommandGoalDialog open onOpenChange={vi.fn()} readOnly onStartGoal={vi.fn()} />);

    expect(screen.getByTestId("goal-condition")).toBeDisabled();
    expect(screen.getByTestId("goal-start")).toBeDisabled();
  });

  it("shows the current goal and clears it", () => {
    const onClearGoal = vi.fn();
    const onOpenChange = vi.fn();
    render(
      <CommandGoalDialog
        open
        onOpenChange={onOpenChange}
        readOnly={false}
        onStartGoal={vi.fn()}
        goal={CLAUDE_GOAL}
        onClearGoal={onClearGoal}
      />,
    );

    const current = screen.getByTestId("goal-current");
    expect(current).toHaveAttribute("data-state", "done");
    expect(current).toHaveTextContent("Complete");
    expect(current).toHaveTextContent("3 checks · 1,200 tokens · 2 min");
    expect(screen.getByTestId("goal-status-detail")).toHaveTextContent(
      "The test run shows 12 passed",
    );
    expect(screen.getByTestId("goal-start")).toHaveTextContent("Replace goal");

    fireEvent.click(screen.getByTestId("goal-clear"));
    expect(onClearGoal).toHaveBeenCalledOnce();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("tells the user how to continue a paused goal", () => {
    render(
      <CommandGoalDialog
        open
        onOpenChange={vi.fn()}
        readOnly={false}
        onStartGoal={vi.fn()}
        goal={{ ...CLAUDE_GOAL, status: "paused", iterations: null, tokensUsed: 0 }}
      />,
    );

    expect(screen.getByTestId("goal-current")).toHaveTextContent("Send a message to continue.");
    expect(screen.queryByTestId("goal-clear")).toBeNull();
  });

  it("formats usage before and after the first goal check", () => {
    const fresh = { ...CLAUDE_GOAL, iterations: null, tokensUsed: 0, timeUsedSeconds: 0 };
    expect(formatCommandGoalUsage(fresh)).toBe("");
    expect(formatCommandGoalUsage({ ...fresh, iterations: 1 })).toBe("1 check");
    expect(formatCommandGoalUsage({ ...fresh, timeUsedSeconds: 125 })).toBe("2 min");
  });
});
