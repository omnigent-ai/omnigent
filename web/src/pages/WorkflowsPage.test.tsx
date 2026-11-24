import { cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import type { WorkflowSessionRequest } from "@/components/workflows/WorkflowSessionDialog";
import {
  type SavedWorkflow,
  useDeleteWorkflow,
  useSavedWorkflows,
  useSharedWorkflows,
} from "@/hooks/useSavedWorkflows";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type * as RoutingModule from "@/lib/routing";

import { WorkflowsPage } from "./WorkflowsPage";
import { useCloneWorkflow, useCreateWorkflowDraft } from "@/hooks/useWorkflow";

vi.mock("@/hooks/useWorkflow", () => ({
  useCloneWorkflow: vi.fn(),
  useCreateWorkflowDraft: vi.fn(),
}));

vi.mock("@/hooks/useSavedWorkflows", () => ({
  useSavedWorkflows: vi.fn(),
  useSharedWorkflows: vi.fn(),
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
    reload: vi.fn(),
    pagination: { hasMore: false, loadingMore: false, loadMore: vi.fn() },
    ...state,
  });
  return refresh;
}

function mockShared(
  workflows: SavedWorkflow[],
  loadMore = vi.fn(),
  hasMore = false,
  state: Partial<ReturnType<typeof useSharedWorkflows>> = {},
) {
  const refresh = vi.fn();
  vi.mocked(useSharedWorkflows).mockReturnValue({
    workflows,
    isLoading: false,
    isError: false,
    refresh,
    reload: vi.fn(),
    pagination: { hasMore, loadingMore: false, loadMore },
    ...state,
  });
  return refresh;
}

const deleteWorkflow = vi.fn();
const createDraft = vi.fn();
const cloneWorkflow = vi.fn();
const rows = () => screen.queryAllByTestId("workflow-row");
const openMenu = (row: HTMLElement) =>
  fireEvent.pointerDown(within(row).getByTestId("workflow-row-menu"), { button: 0 });

describe("WorkflowsPage", () => {
  beforeEach(() => {
    vi.mocked(showToast).mockClear();
    navigate.mockClear();
    deleteWorkflow.mockClear();
    cloneWorkflow.mockClear();
    vi.mocked(useCloneWorkflow).mockReturnValue({
      mutate: cloneWorkflow,
      isPending: false,
    } as unknown as ReturnType<typeof useCloneWorkflow>);
    createDraft.mockReset().mockResolvedValue({ id: "draft-id" });
    vi.mocked(useCreateWorkflowDraft).mockReturnValue({
      mutateAsync: createDraft,
    } as unknown as ReturnType<typeof useCreateWorkflowDraft>);
    vi.mocked(useDeleteWorkflow).mockReturnValue({
      mutate: deleteWorkflow,
    } as unknown as ReturnType<typeof useDeleteWorkflow>);
    mockShared([]);
  });
  afterEach(cleanup);

  it("lists workflows shared with the viewer apart, offering Run and Clone only", () => {
    const shared: SavedWorkflow = {
      ...workflow("Weekly report", "Summarizes the week's changes."),
      viewer_role: "viewer",
      owner_display_name: "Alice",
    };
    const loadMore = vi.fn();
    mockWorkflows({ workflows: WORKFLOWS });
    mockShared([shared], loadMore, true);
    render(<WorkflowsPage />);

    const section = within(screen.getByTestId("workflows-shared"));
    expect(section.getByRole("heading", { name: "Shared with you" })).toBeInTheDocument();
    const [row] = section.getAllByTestId("workflow-row");
    expect(within(row!).getByText("Weekly report")).toBeInTheDocument();
    expect(within(row!).getByTestId("workflow-row-owner")).toHaveTextContent("Shared by Alice");
    expect(within(screen.getByTestId("workflows-list")).queryByText("Weekly report")).toBeNull();

    openMenu(row!);
    expect(screen.queryByTestId("workflow-delete")).toBeNull();
    expect(screen.queryByTestId("workflow-create-automation")).toBeNull();
    fireEvent.click(screen.getByTestId("workflow-clone"));
    expect(cloneWorkflow).toHaveBeenCalledWith("weekly-report", expect.anything());
    fireEvent.click(section.getByRole("button", { name: "Load more shared workflows" }));
    expect(loadMore).toHaveBeenCalledOnce();

    // The search covers both lists.
    fireEvent.change(screen.getByTestId("workflows-search"), { target: { value: "weekly" } });
    expect(screen.queryByTestId("workflows-list")).toBeNull();
    expect(
      within(screen.getByTestId("workflows-shared")).getAllByTestId("workflow-row"),
    ).toHaveLength(1);
  });

  it("keeps loading more shared workflows reachable while no loaded one shows", () => {
    const loadMore = vi.fn();
    const shared = {
      ...workflow("Weekly report", "Summarizes."),
      viewer_role: "viewer" as const,
    };
    // A page of only deleted workflows comes back empty, with a cursor.
    mockWorkflows({ workflows: [] });
    mockShared([], loadMore, true);
    const { unmount } = render(<WorkflowsPage />);
    expect(screen.queryByTestId("workflows-empty-state")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Load more shared workflows" }));
    expect(loadMore).toHaveBeenCalledOnce();
    unmount();

    // A search that matches nothing loaded yet.
    mockWorkflows({ workflows: WORKFLOWS });
    mockShared([shared], loadMore, true);
    render(<WorkflowsPage />);
    fireEvent.change(screen.getByTestId("workflows-search"), { target: { value: "deploy" } });
    expect(screen.queryAllByTestId("workflow-row")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Load more shared workflows" }));
    expect(loadMore).toHaveBeenCalledTimes(2);
  });

  it("shows each list's own failure with a Retry for that list, keeping the other", () => {
    const shared = {
      ...workflow("Weekly report", "Summarizes."),
      viewer_role: "viewer" as const,
    };
    const refreshOwned = mockWorkflows({ workflows: WORKFLOWS });
    const refreshShared = mockShared([], vi.fn(), false, { isError: true });
    const { unmount } = render(<WorkflowsPage />);
    expect(rows()).toHaveLength(2);
    expect(screen.queryByTestId("workflows-empty-state")).toBeNull();
    const failed = screen.getByTestId("workflows-shared-load-error");
    expect(failed).toHaveTextContent("Couldn’t load the workflows shared with you.");
    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    expect(refreshShared).toHaveBeenCalledOnce();
    expect(refreshOwned).not.toHaveBeenCalled();
    unmount();

    const refreshOwnedAgain = mockWorkflows({ workflows: [], isError: true });
    const refreshSharedAgain = mockShared([shared]);
    render(<WorkflowsPage />);
    expect(
      within(screen.getByTestId("workflows-shared")).getAllByTestId("workflow-row"),
    ).toHaveLength(1);
    fireEvent.click(
      within(screen.getByTestId("workflows-load-error")).getByRole("button", { name: "Retry" }),
    );
    expect(refreshOwnedAgain).toHaveBeenCalledOnce();
    expect(refreshSharedAgain).not.toHaveBeenCalled();
  });

  it("waits for the shared list before saying there are no workflows", () => {
    mockWorkflows({ workflows: [] });
    mockShared([], vi.fn(), false, { isLoading: true });
    render(<WorkflowsPage />);
    expect(screen.queryByTestId("workflows-empty-state")).toBeNull();
  });

  it("shows only what is shared when the viewer has no workflows of their own", () => {
    mockWorkflows({ workflows: [] });
    mockShared([{ ...workflow("Weekly report", "Summarizes."), viewer_role: "viewer" }]);
    render(<WorkflowsPage />);
    expect(screen.queryByTestId("workflows-empty-state")).toBeNull();
    expect(
      within(screen.getByTestId("workflows-shared")).getAllByTestId("workflow-row"),
    ).toHaveLength(1);
  });

  it("lists workflows with their descriptions", () => {
    const loadMore = vi.fn();
    mockWorkflows({
      workflows: WORKFLOWS,
      pagination: { hasMore: true, loadingMore: false, loadMore },
    });
    render(<WorkflowsPage />);

    expect(screen.getByRole("heading", { name: "Workflows" })).toBeInTheDocument();
    expect(rows()).toHaveLength(2);
    const first = within(rows()[0]!);
    expect(first.getByText("Fix flaky tests")).toBeInTheDocument();
    expect(
      first.getByText("Reruns the suite and files an issue for each flaky test."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more workflows" }));
    expect(loadMore).toHaveBeenCalledOnce();
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

  it("opens the cloned workflow and reports cloning errors", () => {
    mockWorkflows({ workflows: WORKFLOWS });
    render(<WorkflowsPage />);

    openMenu(rows()[0]!);
    fireEvent.click(screen.getByTestId("workflow-clone"));
    expect(cloneWorkflow).toHaveBeenCalledWith("fix-flaky-tests", expect.anything());
    const { onSuccess, onError } = cloneWorkflow.mock.calls[0]![1];
    onSuccess({ id: "copy-id" });
    expect(navigate).toHaveBeenCalledWith("/workflows/copy-id");
    onError(new Error("Request failed"));
    expect(showToast).toHaveBeenCalledWith("Couldn’t clone workflow. Request failed");
  });

  it.each(["loading", false, true, undefined] as const)(
    "gates New automation on availability (%s), locks the workflow and opens what it creates",
    (automations) => {
      mockWorkflows({ workflows: WORKFLOWS });
      const { rerender } = render(
        <CapabilitiesProvider
          info={
            automations === "loading"
              ? "loading"
              : {
                  ...SERVER_INFO_OFFLINE_FALLBACK,
                  automations_enabled: automations,
                  features: { automations_v2: true },
                }
          }
        >
          <WorkflowsPage />
        </CapabilitiesProvider>,
      );
      expect(screen.queryByTestId("new-automation-dialog")).toBeNull();

      openMenu(rows()[1]!);
      if (automations === "loading" || automations === false) {
        expect(screen.queryByTestId("workflow-create-automation")).toBeNull();
        expect(screen.getByTestId("workflow-run")).not.toHaveAttribute("aria-disabled", "true");
        expect(screen.getByTestId("workflow-delete")).not.toHaveAttribute("aria-disabled", "true");
        return;
      }
      fireEvent.click(screen.getByTestId("workflow-create-automation"));
      const dialog = screen.getByTestId("new-automation-dialog");
      expect(dialog).toHaveTextContent("Release a new version");
      expect(dialog).toHaveAttribute("data-name", "Release a new version");

      fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
      expect(showToast).toHaveBeenCalledWith("Automation created");
      expect(navigate).toHaveBeenCalledWith("/tasks/task-1");
      rerender(
        <CapabilitiesProvider
          info={{ ...SERVER_INFO_OFFLINE_FALLBACK, automations_enabled: false }}
        >
          <WorkflowsPage />
        </CapabilitiesProvider>,
      );
      expect(screen.queryByTestId("new-automation-dialog")).toBeNull();
    },
  );

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
    render(
      <CapabilitiesProvider info={SERVER_INFO_OFFLINE_FALLBACK}>
        <WorkflowsPage />
      </CapabilitiesProvider>,
    );
    expect(screen.getByText("Draft · No saved plan")).toBeInTheDocument();
    openMenu(rows()[0]!);
    expect(screen.getByTestId("workflow-run")).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByTestId("workflow-create-automation")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });
});
