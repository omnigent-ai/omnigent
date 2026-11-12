import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import {
  type SavedWorkflow,
  useDeleteWorkflow,
  useSavedWorkflows,
} from "@/hooks/useSavedWorkflows";
import type * as RoutingModule from "@/lib/routing";
import { createWorkflowPrompt } from "@/lib/workflow";

import { WorkflowsPage } from "./WorkflowsPage";

vi.mock("@/hooks/useSavedWorkflows", () => ({
  useSavedWorkflows: vi.fn(),
  useDeleteWorkflow: vi.fn(),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
const navigate = vi.fn();
vi.mock("@/lib/routing", async (importOriginal) => {
  const actual = await importOriginal<typeof RoutingModule>();
  return { ...actual, useNavigate: () => navigate };
});

function workflow(name: string, description: string): SavedWorkflow {
  return { id: name.toLowerCase().replaceAll(" ", "-"), name, description };
}

const WORKFLOWS = [
  workflow("Fix flaky tests", "Reruns the suite and files an issue for each flaky test."),
  workflow("Release a new version", "Tags the release and drafts the notes."),
];

function mockWorkflows(state: Partial<ReturnType<typeof useSavedWorkflows>>) {
  const refresh = vi.fn();
  vi.mocked(useSavedWorkflows).mockReturnValue({
    workflows: [],
    isLoading: false,
    isError: false,
    refresh,
    ...state,
  });
  return refresh;
}

const deleteWorkflow = vi.fn();
const rows = () => screen.queryAllByTestId("workflow-row");
const openMenu = (row: HTMLElement) =>
  fireEvent.pointerDown(within(row).getByTestId("workflow-row-menu"), { button: 0 });

describe("WorkflowsPage", () => {
  beforeEach(() => {
    vi.mocked(showToast).mockClear();
    navigate.mockClear();
    deleteWorkflow.mockClear();
    vi.mocked(useDeleteWorkflow).mockReturnValue({
      mutate: deleteWorkflow,
    } as unknown as ReturnType<typeof useDeleteWorkflow>);
  });
  afterEach(cleanup);

  it("lists workflows with their descriptions", () => {
    mockWorkflows({ workflows: WORKFLOWS });
    render(<WorkflowsPage />);

    expect(screen.getByRole("heading", { name: "Workflows" })).toBeInTheDocument();
    expect(rows()).toHaveLength(2);
    const first = within(rows()[0]!);
    expect(first.getByText("Fix flaky tests")).toBeInTheDocument();
    expect(
      first.getByText("Reruns the suite and files an issue for each flaky test."),
    ).toBeInTheDocument();
  });

  it("deletes a workflow from its menu, and says when that fails", () => {
    mockWorkflows({ workflows: WORKFLOWS });
    render(<WorkflowsPage />);

    openMenu(rows()[0]!);
    fireEvent.click(screen.getByTestId("workflow-delete"));
    expect(deleteWorkflow).toHaveBeenCalledWith("fix-flaky-tests", expect.anything());

    const { onError } = deleteWorkflow.mock.calls[0]![1] as { onError: () => void };
    onError();
    expect(showToast).toHaveBeenCalledWith("Couldn’t delete “Fix flaky tests”.");
  });

  it("says Run, Edit and Create automation aren't connected yet", () => {
    mockWorkflows({ workflows: WORKFLOWS });
    render(<WorkflowsPage />);

    openMenu(rows()[0]!);
    fireEvent.click(screen.getByTestId("workflow-run"));
    expect(showToast).toHaveBeenLastCalledWith("Running workflows isn’t connected yet.");

    openMenu(rows()[0]!);
    fireEvent.click(screen.getByTestId("workflow-edit"));
    expect(showToast).toHaveBeenLastCalledWith("Editing workflows isn’t connected yet.");

    openMenu(rows()[0]!);
    fireEvent.click(screen.getByTestId("workflow-create-automation"));
    expect(showToast).toHaveBeenLastCalledWith(
      "Creating automations from workflows isn’t connected yet.",
    );
  });

  it("searches names and descriptions", () => {
    mockWorkflows({ workflows: WORKFLOWS });
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
    mockWorkflows({ workflows: [] });
    render(<WorkflowsPage />);

    expect(screen.getByTestId("workflows-empty-state")).toHaveTextContent("No workflows yet");
    expect(screen.queryByTestId("workflows-search")).toBeNull();
  });

  it("shows loading, and a load error with Retry", () => {
    mockWorkflows({ isLoading: true });
    const { unmount } = render(<WorkflowsPage />);
    expect(screen.getByText("Loading workflows…")).toBeInTheDocument();
    unmount();

    const refresh = mockWorkflows({ isError: true });
    render(<WorkflowsPage />);
    const error = screen.getByTestId("workflows-load-error");
    expect(error).toHaveTextContent("Couldn’t load workflows.");
    fireEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("creates a workflow in a new session that drafts it", () => {
    mockWorkflows({ workflows: WORKFLOWS });
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

    expect(navigate).toHaveBeenCalledWith(
      `/?prompt=${encodeURIComponent(
        createWorkflowPrompt("Upgrade a dependency", "Bump one package and fix what breaks"),
      )}`,
    );
    expect(screen.queryByRole("dialog", { name: "New workflow" })).toBeNull();
  });
});
