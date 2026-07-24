import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommandGoalDialog } from "./CommandGoalDialog";

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
});
