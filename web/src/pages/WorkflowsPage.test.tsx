import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import { type WorkflowSummary, type WorkflowsQuery, useWorkflows } from "@/hooks/useWorkflows";

import { WorkflowsPage } from "./WorkflowsPage";

vi.mock("@/hooks/useWorkflows", () => ({ useWorkflows: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const HOUR_S = 60 * 60;

function workflow(name: string, description: string, ageS: number): WorkflowSummary {
  const nowS = Math.floor(Date.now() / 1000);
  return {
    id: name.toLowerCase().replaceAll(" ", "-"),
    name,
    description,
    created_at: nowS - ageS,
    updated_at: nowS - ageS,
  };
}

const WORKFLOWS = [
  workflow(
    "Fix flaky tests",
    "Reruns the suite and files an issue for each flaky test.",
    2.5 * HOUR_S,
  ),
  workflow("Release a new version", "Tags the release and drafts the notes.", 3.5 * 24 * HOUR_S),
];

function mockWorkflows(query: Partial<WorkflowsQuery>) {
  const refetch = vi.fn();
  vi.mocked(useWorkflows).mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: false,
    refetch,
    ...query,
  });
  return refetch;
}

const rows = () => screen.queryAllByTestId("workflow-row");

describe("WorkflowsPage", () => {
  beforeEach(() => vi.mocked(showToast).mockClear());
  afterEach(cleanup);

  it("lists workflows with their descriptions", () => {
    mockWorkflows({ data: WORKFLOWS });
    render(<WorkflowsPage />);

    expect(screen.getByRole("heading", { name: "Workflows" })).toBeInTheDocument();
    expect(rows()).toHaveLength(2);
    const first = within(rows()[0]!);
    expect(first.getByText("Fix flaky tests")).toBeInTheDocument();
    expect(
      first.getByText("Reruns the suite and files an issue for each flaky test."),
    ).toBeInTheDocument();
  });

  it("says the row actions aren't connected yet", () => {
    mockWorkflows({ data: WORKFLOWS });
    render(<WorkflowsPage />);
    const openFirstMenu = () =>
      fireEvent.pointerDown(within(rows()[0]!).getByTestId("workflow-row-menu"), { button: 0 });

    openFirstMenu();
    fireEvent.click(screen.getByTestId("workflow-run"));
    expect(showToast).toHaveBeenLastCalledWith("Running workflows isn’t connected yet.");

    openFirstMenu();
    fireEvent.click(screen.getByTestId("workflow-edit"));
    expect(showToast).toHaveBeenLastCalledWith("Editing workflows isn’t connected yet.");

    openFirstMenu();
    fireEvent.click(screen.getByTestId("workflow-create-automation"));
    expect(showToast).toHaveBeenLastCalledWith(
      "Creating automations from workflows isn’t connected yet.",
    );

    openFirstMenu();
    fireEvent.click(screen.getByTestId("workflow-delete"));
    expect(showToast).toHaveBeenLastCalledWith("Deleting workflows isn’t connected yet.");
  });

  it("searches names and descriptions", () => {
    mockWorkflows({ data: WORKFLOWS });
    render(<WorkflowsPage />);

    fireEvent.change(screen.getByTestId("workflows-search"), { target: { value: "release" } });
    expect(rows()).toHaveLength(1);
    expect(within(rows()[0]!).getByText("Release a new version")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("workflows-search"), { target: { value: "FLAKY TEST" } });
    expect(rows()).toHaveLength(1);
    expect(within(rows()[0]!).getByText("Fix flaky tests")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("workflows-search"), { target: { value: "deploy" } });
    expect(rows()).toHaveLength(0);
    expect(screen.getByTestId("workflows-empty-state")).toHaveTextContent("No workflows found");
  });

  it("invites creating one when there are none, without a search", () => {
    mockWorkflows({ data: [] });
    render(<WorkflowsPage />);

    expect(screen.getByTestId("workflows-empty-state")).toHaveTextContent("No workflows yet");
    expect(screen.queryByTestId("workflows-search")).toBeNull();
  });

  it("shows loading, and a load error with Retry", () => {
    mockWorkflows({ isLoading: true });
    const { unmount } = render(<WorkflowsPage />);
    expect(screen.getByText("Loading workflows…")).toBeInTheDocument();
    unmount();

    const refetch = mockWorkflows({ isError: true });
    render(<WorkflowsPage />);
    const error = screen.getByTestId("workflows-load-error");
    expect(error).toHaveTextContent("Couldn’t load workflows.");
    fireEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("opens New workflow, which checks names against the list", () => {
    mockWorkflows({ data: WORKFLOWS });
    render(<WorkflowsPage />);

    fireEvent.click(screen.getByTestId("new-workflow-button"));
    const dialog = screen.getByRole("dialog", { name: "New workflow" });
    fireEvent.change(within(dialog).getByTestId("new-workflow-name"), {
      target: { value: "Fix flaky tests" },
    });
    expect(within(dialog).getByRole("status")).toHaveTextContent(
      "You already have a workflow with this name.",
    );

    fireEvent.change(within(dialog).getByTestId("new-workflow-name"), {
      target: { value: "Upgrade a dependency" },
    });
    fireEvent.change(within(dialog).getByTestId("new-workflow-description"), {
      target: { value: "Bump one package and fix what breaks" },
    });
    fireEvent.click(within(dialog).getByTestId("new-workflow-create"));

    expect(showToast).toHaveBeenCalledWith("Creating workflows isn’t connected yet.");
    expect(screen.queryByRole("dialog", { name: "New workflow" })).toBeNull();
  });
});
