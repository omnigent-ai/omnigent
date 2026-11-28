import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SaveWorkflowDialog, type SaveWorkflowDialogProps } from "./SaveWorkflowDialog";

function renderDialog(props: Partial<SaveWorkflowDialogProps> = {}) {
  const handlers = { onOpenChange: vi.fn(), onSave: vi.fn() };
  render(<SaveWorkflowDialog open {...handlers} {...props} />);
  return handlers;
}

const name = () => screen.getByTestId("save-workflow-dialog-name");
const save = () => screen.getByTestId("save-workflow-dialog-save");

describe("SaveWorkflowDialog", () => {
  afterEach(cleanup);

  it("starts from the plan's name and saves the name the user picks", () => {
    const { onSave, onOpenChange } = renderDialog({ initialName: "Remove the old flags" });
    expect(screen.getByRole("dialog", { name: "Save as workflow" })).toBeInTheDocument();
    expect(name()).toHaveValue("Remove the old flags");

    fireEvent.change(name(), { target: { value: "  Remove a feature flag  " } });
    fireEvent.click(save());

    expect(onSave).toHaveBeenCalledWith("Remove a feature flag");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("needs a name, and waits while the session can't take the request", () => {
    renderDialog({ initialName: " " });
    expect(save()).toBeDisabled();
    cleanup();

    renderDialog({ initialName: "Triage", blockedReason: "Answer the agent's question first." });
    expect(save()).toBeDisabled();
    expect(screen.getByText("Answer the agent's question first.")).toBeInTheDocument();
  });
});
