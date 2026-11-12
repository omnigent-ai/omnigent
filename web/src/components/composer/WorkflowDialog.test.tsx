import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkflowDialog, type WorkflowDialogProps } from "./WorkflowDialog";

function renderDialog(props: Partial<WorkflowDialogProps> = {}) {
  const handlers = { onOpenChange: vi.fn(), onRunWorkflow: vi.fn() };
  const result = render(<WorkflowDialog open {...handlers} {...props} />);
  return { ...handlers, ...result };
}

const description = () => screen.getByTestId("workflow-dialog-description");
const review = () => screen.getByTestId("workflow-dialog-review-plan");
const run = () => screen.getByTestId("workflow-dialog-run");

describe("WorkflowDialog", () => {
  afterEach(cleanup);

  it("asks for a description, with the plan review checked by default", () => {
    const { onRunWorkflow, onOpenChange } = renderDialog();
    expect(screen.getByRole("dialog", { name: "Run a workflow" })).toBeInTheDocument();
    expect(review()).toHaveAttribute("aria-checked", "true");
    expect(run()).toBeDisabled();

    fireEvent.change(description(), { target: { value: "  Add retries  " } });
    fireEvent.click(run());

    expect(onRunWorkflow).toHaveBeenCalledWith({
      description: "Add retries",
      reviewPlanFirst: true,
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("runs without the review when unchecked, and from Cmd+Enter", () => {
    const { onRunWorkflow } = renderDialog();
    fireEvent.change(description(), { target: { value: "Add retries" } });
    fireEvent.click(review());
    fireEvent.keyDown(description(), { key: "Enter", metaKey: true });

    expect(onRunWorkflow).toHaveBeenCalledWith({
      description: "Add retries",
      reviewPlanFirst: false,
    });
  });

  it("runs a saved workflow with optional details, without the plan review by default", () => {
    const savedWorkflow = {
      id: "0123456789abcdef0123456789abcdef",
      name: "Triage flaky tests",
      description: "Finds and fixes flaky tests.\nGive it: the test suite.",
    };
    const { onRunWorkflow, rerender, onOpenChange } = renderDialog({ savedWorkflow });
    expect(screen.getByRole("dialog", { name: "Run Triage flaky tests" })).toBeInTheDocument();
    const about = screen.getByTestId("workflow-dialog-about");
    expect(about).toHaveTextContent("Give it: the test suite.");
    // A long description shows three lines; the tooltip has all of it.
    expect(about).toHaveClass("line-clamp-3");
    expect(about).toHaveAttribute("title", savedWorkflow.description);
    expect(screen.getByLabelText("Details")).toBe(description());
    expect(review()).toHaveAttribute("aria-checked", "false");
    expect(run()).toBeEnabled();

    fireEvent.change(description(), { target: { value: " The upload tests " } });
    fireEvent.click(run());

    expect(onRunWorkflow).toHaveBeenCalledWith({
      description: "The upload tests",
      reviewPlanFirst: false,
      savedWorkflow,
    });

    // Checked on one run, it starts unchecked again on the next.
    fireEvent.click(review());
    const handlers = { onOpenChange, onRunWorkflow };
    rerender(<WorkflowDialog open={false} {...handlers} savedWorkflow={savedWorkflow} />);
    rerender(<WorkflowDialog open {...handlers} savedWorkflow={savedWorkflow} />);
    expect(review()).toHaveAttribute("aria-checked", "false");
  });

  it("opens on the caller's description, checked again, each time", () => {
    const { rerender, onOpenChange, onRunWorkflow } = renderDialog({ initialDescription: "Draft" });
    expect(description()).toHaveValue("Draft");
    fireEvent.change(description(), { target: { value: "Edited" } });
    fireEvent.click(review());

    const handlers = { onOpenChange, onRunWorkflow };
    rerender(<WorkflowDialog open={false} {...handlers} initialDescription="Draft" />);
    rerender(<WorkflowDialog open {...handlers} initialDescription="Draft" />);

    expect(description()).toHaveValue("Draft");
    expect(review()).toHaveAttribute("aria-checked", "true");
  });

  it("explains and blocks a workflow the session can't take yet", () => {
    const { onRunWorkflow } = renderDialog({ blockedReason: "Answer the agent's question first." });
    fireEvent.change(description(), { target: { value: "Add retries" } });

    expect(screen.getByText("Answer the agent's question first.")).toBeInTheDocument();
    expect(run()).toBeDisabled();
    fireEvent.keyDown(description(), { key: "Enter", ctrlKey: true });
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
    fireEvent.change(description(), { target: { value: "Add retries" } });
    fireEvent.click(run());

    expect(onRunWorkflow).toHaveBeenCalledOnce();
    expect(outerSubmit).not.toHaveBeenCalled();
  });
});
