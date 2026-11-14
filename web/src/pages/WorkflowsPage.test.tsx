import { cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import type { WorkflowSessionRequest } from "@/components/workflows/WorkflowSessionDialog";
import {
  type SavedWorkflow,
  useDeleteWorkflow,
  useSavedWorkflows,
} from "@/hooks/useSavedWorkflows";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type * as RoutingModule from "@/lib/routing";

import { WorkflowsPage } from "./WorkflowsPage";
import { useCreateWorkflowDraft } from "@/hooks/useWorkflow";

vi.mock("@/hooks/useWorkflow", () => ({ useCreateWorkflowDraft: vi.fn() }));

vi.mock("@/hooks/useSavedWorkflows", () => ({
  useSavedWorkflows: vi.fn(),
  useDeleteWorkflow: vi.fn(),
}));
// The dialog has its own tests; here it only shows what it would start a session for.
vi.mock("@/components/workflows/WorkflowSessionDialog", () => ({
  WorkflowSessionDialog: (props: { request: WorkflowSessionRequest | null; open: boolean }) =>
    props.open ? (
      <div data-testid="workflow-session-dialog">{JSON.stringify(props.request)}</div>
    ) : null,
}));
// Likewise New automation: it shows the workflow it locks, and Create reports a new automation.
vi.mock("@/components/scheduled/CreateScheduledTaskDialog", () => ({
  CreateScheduledTaskDialog: (props: {
    open: boolean;
    initialName?: string;
    lockedWorkflow?: SavedWorkflow | null;
    onCreated?: (task: { id: string }) => void;
  }) =>
    props.open ? (
      <div data-testid="new-automation-dialog" data-name={props.initialName}>
        {props.lockedWorkflow?.name}
        <button type="button" onClick={() => props.onCreated?.({ id: "task-1" })}>
          Create
        </button>
      </div>
    ) : null,
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
const createDraft = vi.fn();
const rows = () => screen.queryAllByTestId("workflow-row");
const openMenu = (row: HTMLElement) =>
  fireEvent.pointerDown(within(row).getByTestId("workflow-row-menu"), { button: 0 });

describe("WorkflowsPage", () => {
  beforeEach(() => {
    vi.mocked(showToast).mockClear();
    navigate.mockClear();
    deleteWorkflow.mockClear();
    createDraft.mockReset().mockResolvedValue({ id: "draft-id" });
    vi.mocked(useCreateWorkflowDraft).mockReturnValue({
      mutateAsync: createDraft,
    } as unknown as ReturnType<typeof useCreateWorkflowDraft>);
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

  it("confirms deleting a workflow from its menu, and says when that fails", () => {
    mockWorkflows({ workflows: WORKFLOWS });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <CapabilitiesProvider
          info={{ ...SERVER_INFO_OFFLINE_FALLBACK, automations_enabled: false }}
        >
          <WorkflowsPage />
        </CapabilitiesProvider>
      </QueryClientProvider>,
    );

    openMenu(rows()[0]!);
    fireEvent.click(screen.getByTestId("workflow-delete"));
    expect(deleteWorkflow).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: "Delete workflow?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete workflow" }));
    expect(deleteWorkflow).toHaveBeenCalledWith("fix-flaky-tests", expect.anything());

    const { onError } = deleteWorkflow.mock.calls[0]![1] as { onError: () => void };
    onError();
    expect(showToast).toHaveBeenCalledWith("Couldn’t delete “Fix flaky tests”.");
  });

  it("opens the Run dialog for the workflow", () => {
    mockWorkflows({ workflows: WORKFLOWS });
    render(<WorkflowsPage />);
    expect(screen.queryByTestId("workflow-session-dialog")).toBeNull();

    openMenu(rows()[1]!);
    fireEvent.click(screen.getByTestId("workflow-run"));

    expect(JSON.parse(screen.getByTestId("workflow-session-dialog").textContent!)).toEqual({
      kind: "run",
      workflow: WORKFLOWS[1],
    });
  });

  it("opens New automation locked to the workflow, then the automation it creates", () => {
    mockWorkflows({ workflows: WORKFLOWS });
    render(
      <CapabilitiesProvider
        info={{ ...SERVER_INFO_OFFLINE_FALLBACK, features: { automations_v2: true } }}
      >
        <WorkflowsPage />
      </CapabilitiesProvider>,
    );
    expect(screen.queryByTestId("new-automation-dialog")).toBeNull();

    openMenu(rows()[1]!);
    fireEvent.click(screen.getByTestId("workflow-create-automation"));
    const dialog = screen.getByTestId("new-automation-dialog");
    expect(dialog).toHaveTextContent("Release a new version");
    expect(dialog).toHaveAttribute("data-name", "Release a new version");

    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(showToast).toHaveBeenCalledWith("Automation created");
    expect(navigate).toHaveBeenCalledWith("/tasks/task-1");
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

  it("saves a draft and opens its workflow page after its name", async () => {
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
    expect(screen.queryByTestId("workflow-session-dialog")).toBeNull();
    fireEvent.click(within(dialog).getByTestId("new-workflow-next"));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/workflows/draft-id"));
    expect(screen.queryByRole("dialog", { name: "New workflow" })).toBeNull();
    expect(screen.queryByTestId("workflow-session-dialog")).toBeNull();
    expect(createDraft).toHaveBeenCalledWith({
      name: "Upgrade a dependency",
      description: "Bump one package and fix what breaks",
    });
  });

  it("lists drafts but disables their execution actions", () => {
    mockWorkflows({ workflows: [{ ...WORKFLOWS[0]!, is_draft: true }] });
    render(<WorkflowsPage />);
    expect(screen.getByText("Draft · No saved plan")).toBeInTheDocument();
    openMenu(rows()[0]!);
    expect(screen.getByTestId("workflow-run")).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByTestId("workflow-create-automation")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });
});
