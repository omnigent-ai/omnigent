// Tests for the project-folder action menu: the kebab and the folder header's
// right-click context menu both expose "Rename project" and "Delete project"
// (parity with a conversation row's context menu), and Rename re-labels the
// project while refusing to merge onto an existing project name. See
// ProjectFolder / ProjectMenuItems / ProjectFolderDialogs in Sidebar.tsx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

// Controllable rename mutation, declared via vi.hoisted so the (hoisted)
// vi.mock factory can reference it. Tests assert on mutate/reset.
const mocks = vi.hoisted(() => ({
  rename: {
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
    error: null as Error | null,
  },
  del: {
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  },
  // Real class so the dialog's `error instanceof ProjectNameTakenError` check
  // works and tests can construct the async-collision error.
  ProjectNameTakenError: class ProjectNameTakenError extends Error {},
}));

// Two projects so "Beta" is a live duplicate for the collision test.
const PROJECT_NAMES = ["Alpha", "Beta"];

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({ mutate: vi.fn(), reset: vi.fn() }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: PROJECT_NAMES }),
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => mocks.del,
  useRenameProject: () => mocks.rename,
  ProjectNameTakenError: mocks.ProjectNameTakenError,
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

// One session filed under "Alpha" so the folder has a member; the folder
// header (and its kebab) renders regardless, but this keeps it realistic.
const CONV: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "Alpha Session",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: { omni_project: "Alpha" },
  permission_level: null,
  status: "idle",
  git_branch: null,
};

function mockConversations(conversations: Conversation[]) {
  const dataResult = {
    data: {
      pages: [
        {
          data: conversations,
          first_id: conversations[0]?.id ?? null,
          last_id: conversations.at(-1)?.id ?? null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>;
  useConvMock.mockImplementation(() => dataResult);
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open={true} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

/** Open the "Alpha" folder's kebab dropdown (Radix opens on pointerdown). */
function openAlphaKebab() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Project actions for Alpha" }), {
    button: 0,
  });
}

beforeEach(() => {
  mocks.rename.mutate.mockReset();
  mocks.rename.reset.mockReset();
  mocks.rename.isPending = false;
  mocks.rename.isError = false;
  mocks.rename.error = null;
  mocks.del.mutate.mockReset();
  mockConversations([CONV]);
});

afterEach(() => {
  cleanup();
});

describe("project folder kebab menu", () => {
  it("offers Rename project and opens the rename dialog", () => {
    renderSidebar();
    openAlphaKebab();

    fireEvent.click(screen.getByTestId("rename-project"));

    const dialog = screen.getByRole("dialog");
    // "Rename project" is both the title and the confirm button's label, so
    // target the heading specifically.
    expect(within(dialog).getByRole("heading", { name: "Rename project" })).toBeInTheDocument();
    // The field is pre-seeded with the current project name.
    expect(within(dialog).getByTestId("rename-project-input")).toHaveValue("Alpha");
  });

  it("still offers Delete project (unchanged behavior)", () => {
    renderSidebar();
    openAlphaKebab();
    expect(screen.getByTestId("delete-project")).toBeInTheDocument();
  });
});

describe("project folder right-click (context menu) parity", () => {
  it("right-clicking the folder header opens a menu with Rename + Delete", () => {
    renderSidebar();

    // Right-click the folder header (its title). ContextMenuTrigger wraps the
    // header, so the native contextmenu event opens the same actions menu.
    fireEvent.contextMenu(screen.getByText("Alpha"));

    // Both project actions are present in the context menu.
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
    expect(screen.getByTestId("delete-project")).toBeInTheDocument();
  });

  it("Rename from the context menu opens the rename dialog", () => {
    renderSidebar();
    fireEvent.contextMenu(screen.getByText("Alpha"));

    fireEvent.click(screen.getByTestId("rename-project"));
    expect(
      within(screen.getByRole("dialog")).getByRole("heading", { name: "Rename project" }),
    ).toBeInTheDocument();
  });
});

describe("rename project — submit + validation", () => {
  it("fires renameProject.mutate with the new name and closes the dialog", () => {
    renderSidebar();
    openAlphaKebab();
    fireEvent.click(screen.getByTestId("rename-project"));

    fireEvent.change(screen.getByTestId("rename-project-input"), {
      target: { value: "Renamed" },
    });
    fireEvent.click(screen.getByTestId("rename-project-confirm"));

    expect(mocks.rename.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.rename.mutate).toHaveBeenCalledWith(
      { from: "Alpha", to: "Renamed" },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
    // Fire the captured onSuccess (mutate is a stub) to drive the close.
    const onSuccess = mocks.rename.mutate.mock.calls[0][1].onSuccess as () => void;
    act(() => onSuccess());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("trims whitespace before submitting", () => {
    renderSidebar();
    openAlphaKebab();
    fireEvent.click(screen.getByTestId("rename-project"));

    fireEvent.change(screen.getByTestId("rename-project-input"), {
      target: { value: "  Trimmed  " },
    });
    fireEvent.click(screen.getByTestId("rename-project-confirm"));

    expect(mocks.rename.mutate).toHaveBeenCalledWith(
      { from: "Alpha", to: "Trimmed" },
      expect.anything(),
    );
  });

  it("blocks renaming onto an existing project name — no mutation, shows an error", () => {
    renderSidebar();
    openAlphaKebab();
    fireEvent.click(screen.getByTestId("rename-project"));

    // "Beta" already exists → collision.
    fireEvent.change(screen.getByTestId("rename-project-input"), {
      target: { value: "Beta" },
    });

    const confirm = screen.getByTestId("rename-project-confirm");
    expect(confirm).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/already exists/i);

    // Clicking the disabled confirm must not fire the mutation.
    fireEvent.click(confirm);
    expect(mocks.rename.mutate).not.toHaveBeenCalled();
  });

  it('blocks a case-insensitive duplicate ("beta" collides with "Beta")', () => {
    renderSidebar();
    openAlphaKebab();
    fireEvent.click(screen.getByTestId("rename-project"));

    fireEvent.change(screen.getByTestId("rename-project-input"), {
      target: { value: "beta" },
    });
    expect(screen.getByTestId("rename-project-confirm")).toBeDisabled();
    expect(mocks.rename.mutate).not.toHaveBeenCalled();
  });

  it("disables confirm when the name is unchanged", () => {
    renderSidebar();
    openAlphaKebab();
    fireEvent.click(screen.getByTestId("rename-project"));

    // Field is pre-seeded with "Alpha" (unchanged) → confirm disabled.
    expect(screen.getByTestId("rename-project-confirm")).toBeDisabled();
  });

  it("surfaces the collision error when the rename's server pre-flight rejects an archived-only name", () => {
    // The sync check only sees non-archived projects (`useProjects`), so an
    // archived-only name passes it and the hook's authoritative pre-flight
    // throws ProjectNameTakenError. The dialog must show the collision message,
    // not the generic partial-failure one.
    mocks.rename.isError = true;
    mocks.rename.error = new mocks.ProjectNameTakenError("Gamma");

    renderSidebar();
    openAlphaKebab();
    fireEvent.click(screen.getByTestId("rename-project"));

    expect(screen.getByRole("alert")).toHaveTextContent(/already exists/i);
    expect(screen.getByRole("alert")).not.toHaveTextContent(/couldn't be renamed/i);
  });
});
