import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerAddMenu } from "./ComposerAddMenu";

const savedWorkflows = [
  {
    id: "a".repeat(32),
    name: "Triage flaky tests",
    description: "Finds and fixes flaky tests.\nGive it: the test suite.",
  },
  { id: "b".repeat(32), name: "Release notes", description: "Drafts the release notes." },
];

function renderMenu(props: Partial<Parameters<typeof ComposerAddMenu>[0]> = {}) {
  const handlers = {
    onAttach: vi.fn(),
    onWorkflow: vi.fn(),
    onRunSavedWorkflow: vi.fn(),
    onOpen: vi.fn(),
  };
  render(
    <ComposerAddMenu
      disabled={false}
      planActive={false}
      showGoal={false}
      showPlan={false}
      {...handlers}
      {...props}
    />,
  );
  fireEvent.pointerDown(screen.getByRole("button", { name: "Add" }), { button: 0 });
  return handlers;
}

const runAWorkflow = () => fireEvent.click(screen.getByTestId("composer-workflow-action"));

describe("ComposerAddMenu's Run a workflow", () => {
  afterEach(cleanup);

  it("opens the description dialog when there are no saved workflows", async () => {
    const { onWorkflow, onOpen } = renderMenu();
    expect(onOpen).toHaveBeenCalled();
    runAWorkflow();
    await waitFor(() => expect(onWorkflow).toHaveBeenCalledWith());
  });

  it("lists the saved workflows to search and run", async () => {
    const { onRunSavedWorkflow, onWorkflow } = renderMenu({ savedWorkflows });
    runAWorkflow();
    const names = () =>
      screen.getAllByTestId("composer-saved-workflow").map((row) => row.textContent);
    expect(names()).toEqual([
      "Triage flaky testsFinds and fixes flaky tests.",
      "Release notesDrafts the release notes.",
    ]);

    fireEvent.change(screen.getByLabelText("Search workflows"), { target: { value: "TRIAGE" } });
    expect(names()).toEqual(["Triage flaky testsFinds and fixes flaky tests."]);
    fireEvent.click(screen.getByTestId("composer-saved-workflow"));

    await waitFor(() => expect(onRunSavedWorkflow).toHaveBeenCalledWith(savedWorkflows[0]));
    expect(onWorkflow).not.toHaveBeenCalled();
  });

  it("describes a new workflow, starting from what was typed", async () => {
    const { onWorkflow } = renderMenu({ savedWorkflows });
    runAWorkflow();
    fireEvent.change(screen.getByLabelText("Search workflows"), { target: { value: "Bump deps" } });
    expect(screen.getByText("No matching workflows")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("composer-describe-workflow"));
    await waitFor(() => expect(onWorkflow).toHaveBeenCalledWith("Bump deps"));
  });
});
