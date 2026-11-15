import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import type * as RoutingModule from "@/lib/routing";

import { WorkflowRow } from "./WorkflowRow";

const navigate = vi.fn();
vi.mock("@/lib/routing", async (importOriginal) => {
  const actual = await importOriginal<typeof RoutingModule>();
  return { ...actual, useNavigate: () => navigate };
});

const WORKFLOW: SavedWorkflow = {
  id: "6f1c2b9a4d8e4f30a1b2c3d4e5f60718",
  name: "Fix flaky tests",
  description: "Reruns the suite and files an issue for each flaky test.",
};

function renderRow(workflow: SavedWorkflow = WORKFLOW, automationsEnabled = true) {
  const handlers = {
    onRun: vi.fn(),
    onCreateAutomation: vi.fn(),
    onDelete: vi.fn(),
  };
  render(
    <WorkflowRow
      workflow={workflow}
      {...handlers}
      onCreateAutomation={automationsEnabled ? handlers.onCreateAutomation : undefined}
    />,
  );
  return handlers;
}

const openMenu = () =>
  fireEvent.pointerDown(screen.getByTestId("workflow-row-menu"), { button: 0 });
const itemNames = () => screen.getAllByRole("menuitem").map((item) => item.textContent);

describe("WorkflowRow", () => {
  afterEach(() => {
    cleanup();
    navigate.mockClear();
  });

  it("opens the workflow's page, but not from its menu", () => {
    renderRow();

    fireEvent.click(screen.getByRole("button", { name: "Open Fix flaky tests" }));
    expect(navigate).toHaveBeenCalledWith(`/workflows/${WORKFLOW.id}`);
    navigate.mockClear();
    openMenu();
    fireEvent.click(screen.getByTestId("workflow-run"));
    expect(navigate).not.toHaveBeenCalled();
  });

  it("shows the name and description, without a time", () => {
    renderRow();

    expect(screen.getByText("Fix flaky tests")).toBeInTheDocument();
    expect(
      screen.getByText("Reruns the suite and files an issue for each flaky test."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Updated/)).toBeNull();
  });

  it.each([false, true])("offers available actions in the ⋯ menu (automations=%s)", (enabled) => {
    const { onRun, onCreateAutomation, onDelete } = renderRow(WORKFLOW, enabled);

    expect(screen.getByRole("button", { name: "Actions for Fix flaky tests" })).toBeInTheDocument();
    openMenu();
    expect(itemNames()).toEqual(
      enabled ? ["Run", "Create automation", "Delete"] : ["Run", "Delete"],
    );
    fireEvent.click(screen.getByTestId("workflow-run"));
    expect(onRun).toHaveBeenCalledWith(WORKFLOW);

    if (enabled) {
      openMenu();
      fireEvent.click(screen.getByTestId("workflow-create-automation"));
      expect(onCreateAutomation).toHaveBeenCalledWith(WORKFLOW);
    } else {
      expect(onCreateAutomation).not.toHaveBeenCalled();
    }

    openMenu();
    fireEvent.click(screen.getByTestId("workflow-delete"));
    expect(onDelete).toHaveBeenCalledWith(WORKFLOW);
  });

  it.each([false, true])("offers the same actions on right-click (automations=%s)", (enabled) => {
    const { onRun } = renderRow(WORKFLOW, enabled);

    fireEvent.contextMenu(screen.getByTestId("workflow-row"));
    expect(itemNames()).toEqual(
      enabled ? ["Run", "Create automation", "Delete"] : ["Run", "Delete"],
    );
    fireEvent.click(screen.getByTestId("workflow-run"));
    expect(onRun).toHaveBeenCalledWith(WORKFLOW);
  });
});
