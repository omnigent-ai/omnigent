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

  it("names the owner of a workflow shared with the user, which runs like the user's own", async () => {
    const shared = {
      id: "d".repeat(32),
      name: "Weekly report",
      description: "Summarizes the week.",
      viewer_role: "viewer" as const,
      owner_display_name: "Alice",
    };
    const { onRunSavedWorkflow } = renderMenu({ savedWorkflows: [...savedWorkflows, shared] });
    runAWorkflow();
    const rows = screen.getAllByTestId("composer-saved-workflow");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Triage flaky testsFinds and fixes flaky tests.",
      "Release notesDrafts the release notes.",
      "Weekly reportShared by Alice · Summarizes the week.",
    ]);
    fireEvent.click(rows[2]!);
    await waitFor(() => expect(onRunSavedWorkflow).toHaveBeenCalledWith(shared));
  });

  it("opens the picker while more workflows can load, though none loaded yet show", async () => {
    const loadMore = vi.fn();
    const { onWorkflow } = renderMenu({
      savedWorkflows: [],
      workflowPagination: { hasMore: true, loadingMore: false, loadMore },
    });
    runAWorkflow();
    expect(await screen.findByTestId("composer-workflow-picker")).toBeInTheDocument();
    expect(onWorkflow).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Load more workflows"));
    expect(loadMore).toHaveBeenCalledOnce();
  });

  it("lists the saved workflows to search and run", async () => {
    const loadMore = vi.fn();
    const { onRunSavedWorkflow, onWorkflow } = renderMenu({
      workflowPagination: { hasMore: true, loadingMore: false, loadMore },
      savedWorkflows: [
        ...savedWorkflows,
        {
          id: "c".repeat(32),
          name: "Unfinished workflow",
          description: "Still planning.",
          is_draft: true,
        },
      ],
    });
    runAWorkflow();
    const names = () =>
      screen.getAllByTestId("composer-saved-workflow").map((row) => row.textContent);
    expect(names()).toEqual([
      "Triage flaky testsFinds and fixes flaky tests.",
      "Release notesDrafts the release notes.",
    ]);
    fireEvent.click(screen.getByText("Load more workflows"));
    expect(loadMore).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("Search workflows")).toBeVisible();
    expect(onRunSavedWorkflow).not.toHaveBeenCalled();

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
