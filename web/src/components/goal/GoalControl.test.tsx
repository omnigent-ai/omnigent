import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Goal } from "@/lib/goalApi";
import { GoalControl, GoalStatusPill } from "./GoalControl";

vi.mock("./GoalDialog", () => ({
  GoalDialog: ({ open, conversationId }: { open: boolean; conversationId: string | null }) => (
    <div data-testid="mock-goal-dialog" data-open={open ? "true" : "false"}>
      {conversationId}
    </div>
  ),
}));

vi.mock("./CommandGoalDialog", () => ({
  CommandGoalDialog: ({
    open,
    onStartGoal,
  }: {
    open: boolean;
    onStartGoal: (condition: string) => void;
  }) => (
    <button
      type="button"
      data-testid="mock-command-goal-dialog"
      data-open={open ? "true" : "false"}
      onClick={() => onStartGoal("All tests pass")}
    >
      Start
    </button>
  ),
}));

const GOAL: Goal = {
  objective: "Ship goal mode",
  status: "active",
  tokenBudget: 40000,
  tokensUsed: 1200,
  timeUsedSeconds: 125,
  createdAt: null,
  updatedAt: null,
};

function renderControl(conversationId: string | null = "conv") {
  return render(
    <TooltipProvider>
      <GoalControl
        conversationId={conversationId}
        readOnly={false}
        goal={GOAL}
        onGoalChange={vi.fn()}
      />
    </TooltipProvider>,
  );
}

afterEach(cleanup);

describe("GoalControl", () => {
  it("opens the dialog from the toolbar button", () => {
    renderControl();

    expect(screen.getByTestId("mock-goal-dialog")).toHaveAttribute("data-open", "false");
    fireEvent.click(screen.getByTestId("goal-toggle"));
    expect(screen.getByTestId("mock-goal-dialog")).toHaveAttribute("data-open", "true");
    expect(screen.getByTestId("goal-toggle")).toHaveAttribute("aria-pressed", "true");
  });

  it("disables the button without a conversation", () => {
    renderControl(null);

    expect(screen.getByTestId("goal-toggle")).toBeDisabled();
  });

  it("collapses the visible Goal label in a narrow composer", () => {
    renderControl();

    const button = screen.getByRole("button", { name: "View goal" });
    expect(button).toHaveClass("w-9", "@lg/composer-actions:w-auto");
    expect(screen.getByText("Goal")).toHaveClass("hidden", "@lg/composer-actions:inline");
  });

  it("starts a command-backed goal", () => {
    const onStartGoal = vi.fn();
    render(
      <TooltipProvider>
        <GoalControl
          mode="command"
          conversationId="conv"
          readOnly={false}
          onStartGoal={onStartGoal}
          backendLabel="Claude"
        />
      </TooltipProvider>,
    );

    fireEvent.click(screen.getByTestId("goal-toggle"));
    expect(screen.getByTestId("mock-command-goal-dialog")).toHaveAttribute("data-open", "true");
    fireEvent.click(screen.getByTestId("mock-command-goal-dialog"));
    expect(onStartGoal).toHaveBeenCalledWith("All tests pass");
  });

  it("renders a working icon while the goal is in progress", () => {
    render(
      <TooltipProvider>
        <GoalStatusPill goal={{ ...GOAL, status: "active" }} />
      </TooltipProvider>,
    );

    const pill = screen.getByTestId("composer-goal-mode");
    expect(pill).toHaveAttribute("data-state", "working");
    expect(pill).toHaveAccessibleName(`Goal active: ${GOAL.objective}`);
  });

  it("renders a done icon once the goal completes", () => {
    render(
      <TooltipProvider>
        <GoalStatusPill goal={{ ...GOAL, status: "complete" }} />
      </TooltipProvider>,
    );

    expect(screen.getByTestId("composer-goal-mode")).toHaveAttribute("data-state", "done");
  });

  it("opens the goal dialog on click", () => {
    const onOpen = vi.fn();
    render(
      <TooltipProvider>
        <GoalStatusPill goal={GOAL} onOpen={onOpen} />
      </TooltipProvider>,
    );

    fireEvent.click(screen.getByTestId("composer-goal-mode"));
    expect(onOpen).toHaveBeenCalledOnce();
  });
});
