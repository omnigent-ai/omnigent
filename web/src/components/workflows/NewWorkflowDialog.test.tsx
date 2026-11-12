import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NewWorkflowDialog, type NewWorkflowDialogProps } from "./NewWorkflowDialog";

function renderDialog(props: Partial<NewWorkflowDialogProps> = {}) {
  const handlers = { onOpenChange: vi.fn(), onCreate: vi.fn() };
  const result = render(<NewWorkflowDialog open {...handlers} {...props} />);
  return { ...handlers, ...result };
}

const name = () => screen.getByTestId("new-workflow-name");
const description = () => screen.getByTestId("new-workflow-description");
const next = () => screen.getByTestId("new-workflow-next");

describe("NewWorkflowDialog", () => {
  afterEach(cleanup);

  it("asks for a name and what the workflow should do", () => {
    const { onCreate, onOpenChange } = renderDialog();

    expect(screen.getByRole("dialog", { name: "New workflow" })).toBeInTheDocument();
    expect(next()).toBeDisabled();
    fireEvent.change(name(), { target: { value: "  Fix flaky tests  " } });
    expect(next()).toBeDisabled();
    fireEvent.change(description(), { target: { value: " Rerun the suite and file issues " } });
    fireEvent.click(next());

    expect(onCreate).toHaveBeenCalledWith({
      name: "Fix flaky tests",
      description: "Rerun the suite and file issues",
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("moves on from Cmd+Enter in the description", () => {
    const { onCreate } = renderDialog();

    fireEvent.change(name(), { target: { value: "Fix flaky tests" } });
    fireEvent.change(description(), { target: { value: "Rerun the suite" } });
    fireEvent.keyDown(description(), { key: "Enter", metaKey: true });

    expect(onCreate).toHaveBeenCalledWith({
      name: "Fix flaky tests",
      description: "Rerun the suite",
    });
  });

  it("blocks a name the user already uses", () => {
    const { onCreate } = renderDialog({ takenNames: ["Fix flaky tests"] });

    fireEvent.change(name(), { target: { value: "Fix flaky tests " } });
    fireEvent.change(description(), { target: { value: "Rerun the suite" } });
    expect(screen.getByRole("status")).toHaveTextContent(
      "You already have a workflow with this name.",
    );
    expect(name()).toHaveAttribute("aria-invalid", "true");
    expect(next()).toBeDisabled();
    fireEvent.keyDown(description(), { key: "Enter", metaKey: true });
    expect(onCreate).not.toHaveBeenCalled();

    fireEvent.change(name(), { target: { value: "Fix flaky tests nightly" } });
    expect(screen.queryByRole("status")).toBeNull();
    expect(next()).toBeEnabled();
  });

  it("starts empty each time it opens", () => {
    const { rerender, onOpenChange, onCreate } = renderDialog();
    fireEvent.change(name(), { target: { value: "Fix flaky tests" } });

    rerender(<NewWorkflowDialog open={false} onOpenChange={onOpenChange} onCreate={onCreate} />);
    rerender(<NewWorkflowDialog open onOpenChange={onOpenChange} onCreate={onCreate} />);

    expect(name()).toHaveValue("");
    expect(description()).toHaveValue("");
  });
});
