import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { CodexGoal } from "@/lib/codexGoalApi";
import { CodexGoalControl, CodexGoalStatusPill } from "./CodexGoalControl";

vi.mock("./CodexGoalDialog", () => ({
  CodexGoalDialog: ({ open, conversationId }: { open: boolean; conversationId: string | null }) => (
    <div data-testid="mock-goal-dialog" data-open={open ? "true" : "false"}>
      {conversationId}
    </div>
  ),
}));

const GOAL: CodexGoal = {
  threadId: "thread-1",
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
      <CodexGoalControl
        conversationId={conversationId}
        readOnly={false}
        goal={GOAL}
        onGoalChange={vi.fn()}
      />
    </TooltipProvider>,
  );
}

afterEach(cleanup);

describe("CodexGoalControl", () => {
  it("opens the dialog from the toolbar button", () => {
    renderControl();

    expect(screen.getByTestId("mock-goal-dialog")).toHaveAttribute("data-open", "false");
    fireEvent.click(screen.getByTestId("codex-goal-toggle"));
    expect(screen.getByTestId("mock-goal-dialog")).toHaveAttribute("data-open", "true");
    expect(screen.getByTestId("codex-goal-toggle")).toHaveAttribute("aria-pressed", "true");
  });

  it("disables the button without a conversation", () => {
    renderControl(null);

    expect(screen.getByTestId("codex-goal-toggle")).toBeDisabled();
  });

  it("renders the status pill", () => {
    render(<CodexGoalStatusPill goal={{ ...GOAL, status: "blocked" }} />);

    expect(screen.getByTestId("composer-goal-mode")).toHaveTextContent("Goal blocked");
  });
});
