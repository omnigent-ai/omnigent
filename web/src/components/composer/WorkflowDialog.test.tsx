import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkflowDialog, type WorkflowDialogProps } from "./WorkflowDialog";

function renderDialog(props: Partial<WorkflowDialogProps> = {}) {
  const handlers = { onOpenChange: vi.fn(), onRunWorkflow: vi.fn() };
  const result = render(<WorkflowDialog open {...handlers} {...props} />);
  return { ...handlers, ...result };
}

const goal = () => screen.getByTestId("workflow-dialog-goal");
const review = () => screen.getByTestId("workflow-dialog-review-plan");
const run = () => screen.getByTestId("workflow-dialog-run");

describe("WorkflowDialog", () => {
  afterEach(cleanup);

  it("asks for a goal, with the plan review checked by default", () => {
    const { onRunWorkflow, onOpenChange } = renderDialog();
    expect(screen.getByRole("dialog", { name: "Run a workflow" })).toBeInTheDocument();
    expect(review()).toHaveAttribute("aria-checked", "true");
    expect(run()).toBeDisabled();

    fireEvent.change(goal(), { target: { value: "  Add retries  " } });
    fireEvent.click(run());

    expect(onRunWorkflow).toHaveBeenCalledWith({ goal: "Add retries", reviewPlanFirst: true });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("runs without the review when unchecked, and from Cmd+Enter", () => {
    const { onRunWorkflow } = renderDialog();
    fireEvent.change(goal(), { target: { value: "Add retries" } });
    fireEvent.click(review());
    fireEvent.keyDown(goal(), { key: "Enter", metaKey: true });

    expect(onRunWorkflow).toHaveBeenCalledWith({ goal: "Add retries", reviewPlanFirst: false });
  });

  it("opens on the caller's goal, checked again, each time", () => {
    const { rerender, onOpenChange, onRunWorkflow } = renderDialog({ initialGoal: "Draft" });
    expect(goal()).toHaveValue("Draft");
    fireEvent.change(goal(), { target: { value: "Edited" } });
    fireEvent.click(review());

    const handlers = { onOpenChange, onRunWorkflow };
    rerender(<WorkflowDialog open={false} {...handlers} initialGoal="Draft" />);
    rerender(<WorkflowDialog open {...handlers} initialGoal="Draft" />);

    expect(goal()).toHaveValue("Draft");
    expect(review()).toHaveAttribute("aria-checked", "true");
  });

  it("explains and blocks a workflow the session can't take yet", () => {
    const { onRunWorkflow } = renderDialog({ blockedReason: "Answer the agent's question first." });
    fireEvent.change(goal(), { target: { value: "Add retries" } });

    expect(screen.getByText("Answer the agent's question first.")).toBeInTheDocument();
    expect(run()).toBeDisabled();
    fireEvent.keyDown(goal(), { key: "Enter", ctrlKey: true });
    expect(onRunWorkflow).not.toHaveBeenCalled();
  });

  it("keeps its submit from reaching a form it renders inside", () => {
    const outerSubmit = vi.fn((event: React.FormEvent) => event.preventDefault());
    const onRunWorkflow = vi.fn();
    render(
      <form onSubmit={outerSubmit}>
        <WorkflowDialog open onOpenChange={vi.fn()} onRunWorkflow={onRunWorkflow} />
      </form>,
    );
    fireEvent.change(goal(), { target: { value: "Add retries" } });
    fireEvent.click(run());

    expect(onRunWorkflow).toHaveBeenCalledOnce();
    expect(outerSubmit).not.toHaveBeenCalled();
  });
});
