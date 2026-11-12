import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkflowSummary } from "@/hooks/useWorkflows";

import { WorkflowRow } from "./WorkflowRow";

const NOW = new Date("2026-11-12T12:00:00Z");
const NOW_S = NOW.getTime() / 1000;

const WORKFLOW: WorkflowSummary = {
  id: "6f1c2b9a4d8e4f30a1b2c3d4e5f60718",
  name: "Fix flaky tests",
  description: "Reruns the suite and files an issue for each flaky test.",
  created_at: NOW_S - 3 * 24 * 3600,
  updated_at: NOW_S - 2 * 3600,
};

function renderRow(workflow: WorkflowSummary = WORKFLOW) {
  const handlers = { onRun: vi.fn(), onEdit: vi.fn(), onCreateAutomation: vi.fn() };
  render(<WorkflowRow workflow={workflow} now={NOW} {...handlers} />);
  return handlers;
}

const openMenu = () =>
  fireEvent.pointerDown(screen.getByTestId("workflow-row-menu"), { button: 0 });
const itemNames = () => screen.getAllByRole("menuitem").map((item) => item.textContent);

describe("WorkflowRow", () => {
  afterEach(cleanup);

  it("shows the name, description and last update", () => {
    renderRow();

    expect(screen.getByText("Fix flaky tests")).toBeInTheDocument();
    expect(
      screen.getByText("Reruns the suite and files an issue for each flaky test."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("workflow-row-updated")).toHaveTextContent("Updated 2h ago");
  });

  it("says just now for a change in the last minute", () => {
    renderRow({ ...WORKFLOW, updated_at: NOW_S - 20 });

    expect(screen.getByTestId("workflow-row-updated")).toHaveTextContent("Updated just now");
  });

  it("offers Run, Edit and Create automation in the ⋯ menu", () => {
    const { onRun, onEdit, onCreateAutomation } = renderRow();

    expect(screen.getByRole("button", { name: "Actions for Fix flaky tests" })).toBeInTheDocument();
    openMenu();
    expect(itemNames()).toEqual(["Run", "Edit", "Create automation"]);
    fireEvent.click(screen.getByTestId("workflow-run"));
    expect(onRun).toHaveBeenCalledWith(WORKFLOW);

    openMenu();
    fireEvent.click(screen.getByTestId("workflow-edit"));
    expect(onEdit).toHaveBeenCalledWith(WORKFLOW);

    openMenu();
    fireEvent.click(screen.getByTestId("workflow-create-automation"));
    expect(onCreateAutomation).toHaveBeenCalledWith(WORKFLOW);
  });

  it("offers the same actions on right-click", () => {
    const { onRun } = renderRow();

    fireEvent.contextMenu(screen.getByTestId("workflow-row"));
    expect(itemNames()).toEqual(["Run", "Edit", "Create automation"]);
    fireEvent.click(screen.getByTestId("workflow-run"));
    expect(onRun).toHaveBeenCalledWith(WORKFLOW);
  });
});
