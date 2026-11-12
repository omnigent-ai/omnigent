import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";

import { WorkflowRow } from "./WorkflowRow";

const WORKFLOW: SavedWorkflow = {
  id: "6f1c2b9a4d8e4f30a1b2c3d4e5f60718",
  name: "Fix flaky tests",
  description: "Reruns the suite and files an issue for each flaky test.",
};

function renderRow(workflow: SavedWorkflow = WORKFLOW) {
  const handlers = {
    onRun: vi.fn(),
    onEdit: vi.fn(),
    onCreateAutomation: vi.fn(),
    onDelete: vi.fn(),
  };
  render(<WorkflowRow workflow={workflow} {...handlers} />);
  return handlers;
}

const openMenu = () =>
  fireEvent.pointerDown(screen.getByTestId("workflow-row-menu"), { button: 0 });
const itemNames = () => screen.getAllByRole("menuitem").map((item) => item.textContent);

describe("WorkflowRow", () => {
  afterEach(cleanup);

  it("shows the name and description, without a time", () => {
    renderRow();

    expect(screen.getByText("Fix flaky tests")).toBeInTheDocument();
    expect(
      screen.getByText("Reruns the suite and files an issue for each flaky test."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Updated/)).toBeNull();
  });

  it("offers Run, Edit, Create automation and Delete in the ⋯ menu", () => {
    const { onRun, onEdit, onCreateAutomation, onDelete } = renderRow();

    expect(screen.getByRole("button", { name: "Actions for Fix flaky tests" })).toBeInTheDocument();
    openMenu();
    expect(itemNames()).toEqual(["Run", "Edit", "Create automation", "Delete"]);
    fireEvent.click(screen.getByTestId("workflow-run"));
    expect(onRun).toHaveBeenCalledWith(WORKFLOW);

    openMenu();
    fireEvent.click(screen.getByTestId("workflow-edit"));
    expect(onEdit).toHaveBeenCalledWith(WORKFLOW);

    openMenu();
    fireEvent.click(screen.getByTestId("workflow-create-automation"));
    expect(onCreateAutomation).toHaveBeenCalledWith(WORKFLOW);

    openMenu();
    fireEvent.click(screen.getByTestId("workflow-delete"));
    expect(onDelete).toHaveBeenCalledWith(WORKFLOW);
  });

  it("offers the same actions on right-click", () => {
    const { onRun } = renderRow();

    fireEvent.contextMenu(screen.getByTestId("workflow-row"));
    expect(itemNames()).toEqual(["Run", "Edit", "Create automation", "Delete"]);
    fireEvent.click(screen.getByTestId("workflow-run"));
    expect(onRun).toHaveBeenCalledWith(WORKFLOW);
  });
});
