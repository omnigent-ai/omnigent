import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

const PROJECT_NAME = "Sprint 42";
const PROJECT_ID = "p_sprint42";

const mocks = vi.hoisted(() => ({
  renameProject: {
    mutateAsync: vi.fn(() => Promise.resolve(PROJECT_ID)),
    isPending: false,
    isError: false,
    error: null,
  },
  deleteProject: {
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
  },
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [] }),
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useLeaveSession: () => ({ mutate: vi.fn(), isPending: false }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkMoveToProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
    variables: undefined,
  }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [{ id: PROJECT_ID, name: PROJECT_NAME }] }),
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => mocks.deleteProject,
  useRenameProject: () => mocks.renameProject,
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useProjectConfig: () => ({ data: {}, isLoading: false, isError: false }),
  useUpdateProjectConfig: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(() => Promise.resolve()),
    isPending: false,
    isError: false,
    error: null,
  }),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("./ProjectSettingsDialog", () => ({
  ProjectSettingsDialog: ({ open }: { open: boolean }) =>
    open ? <div role="dialog">Project settings</div> : null,
}));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConversationsMock = vi.mocked(useConversations);

const FILED_CONVERSATION: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "My Session",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: {},
  project_id: PROJECT_ID,
  permission_level: null,
  status: "idle",
};

function mockConversations(conversations: Conversation[]) {
  const result = {
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
  useConversationsMock.mockImplementation(() => result);
}

function renderSidebar() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function folderHeader(): HTMLElement {
  const header = Array.from(document.querySelectorAll("h2 button")).find(
    (button) => button.textContent === PROJECT_NAME,
  );
  if (header === undefined) throw new Error(`No folder header for ${PROJECT_NAME}`);
  return header as HTMLElement;
}

function contextTrigger(): HTMLElement {
  const trigger = folderHeader().closest('[data-slot="context-menu-trigger"]');
  if (trigger === null) throw new Error("Folder header has no context-menu trigger");
  return trigger as HTMLElement;
}

function dismissMenu() {
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
}

function dispatchTouchPointer(
  target: HTMLElement,
  type: "pointerdown" | "pointermove" | "pointerup",
) {
  const event = new PointerEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: type === "pointerdown" ? 10 : 11,
    clientY: 10,
    pointerType: "touch",
  });
  expect(event.pointerType).toBe("touch");
  fireEvent(target, event);
}

function expectProjectMenuActions() {
  expect(screen.getByTestId("project-new-session-menu")).toBeInTheDocument();
  expect(screen.getByTestId("rename-project")).toBeInTheDocument();
  expect(screen.getByTestId("project-settings")).toBeInTheDocument();
  expect(screen.getByTestId("delete-project")).toBeInTheDocument();
}

beforeEach(() => {
  vi.clearAllMocks();
  useConversationsMock.mockReset();
  localStorage.clear();
  mockConversations([FILED_CONVERSATION]);
});

afterEach(cleanup);

describe("project folder header context menu", () => {
  it("opens the kebab's exact action set on right-click", () => {
    renderSidebar();
    const header = folderHeader();

    expect(screen.queryByTestId("rename-project")).toBeNull();
    fireEvent.contextMenu(header);

    expectProjectMenuActions();
    expect(header).toHaveAttribute("data-slot", "context-menu-trigger");
  });

  it("carries exactly the same items as the kebab", () => {
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("project-actions"), { button: 0 });
    const kebabItems = screen
      .getAllByRole("menuitem")
      .map((item) => item.getAttribute("data-testid"));

    cleanup();
    renderSidebar();
    fireEvent.contextMenu(folderHeader());
    const contextItems = screen
      .getAllByRole("menuitem")
      .map((item) => item.getAttribute("data-testid"));

    expect(contextItems).toEqual(kebabItems);
  });

  it("keeps the kebab working", () => {
    renderSidebar();

    expect(contextTrigger()).toBe(folderHeader());
    fireEvent.pointerDown(screen.getByTestId("project-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("rename-project"));

    expect(screen.getByTestId("rename-project-confirm")).toBeInTheDocument();
  });

  it("drives Rename into the shared dialog and mutation", async () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("rename-project"));
    fireEvent.change(screen.getByDisplayValue(PROJECT_NAME), {
      target: { value: "Sprint 43" },
    });
    fireEvent.click(screen.getByTestId("rename-project-confirm"));

    await waitFor(() =>
      expect(mocks.renameProject.mutateAsync).toHaveBeenCalledWith({
        id: PROJECT_ID,
        oldName: PROJECT_NAME,
        newName: "Sprint 43",
      }),
    );
  });

  it("drives Project settings from the context menu", () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("project-settings"));

    expect(screen.getByRole("dialog")).toHaveTextContent("Project settings");
  });

  it("drives Delete into the shared confirmation and mutation", () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("delete-project"));
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    expect(mocks.deleteProject.mutate).toHaveBeenCalledWith(
      { id: PROJECT_ID, name: PROJECT_NAME },
      expect.anything(),
    );
  });

  it("keeps New session reachable when a touch opens the menu on hover-capable hardware", () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    const item = screen.getByTestId("project-new-session-menu");

    expect(item).toHaveAttribute("href", `/?project=${encodeURIComponent(PROJECT_NAME)}`);
    for (const hiddenClass of [
      "hidden",
      "md:hidden",
      "[@media((hover:hover)_and_(pointer:fine))]:md:hidden",
    ]) {
      expect(item).not.toHaveClass(hiddenClass);
    }
  });

  it("still expands and collapses on plain left-click", () => {
    renderSidebar();

    expect(contextTrigger()).toBe(folderHeader());
    expect(folderHeader()).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(folderHeader());
    expect(folderHeader()).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(folderHeader());
    expect(folderHeader()).toHaveAttribute("aria-expanded", "false");
  });

  it("toggles on the first left-click after a mouse-opened menu is dismissed", () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
    expect(folderHeader()).toHaveAttribute("aria-expanded", "false");
    dismissMenu();
    fireEvent.click(folderHeader());

    expect(folderHeader()).toHaveAttribute("aria-expanded", "true");
  });

  it("guards touch selection without a viewport breakpoint", () => {
    renderSidebar();
    const header = folderHeader();

    expect(header).toHaveClass("select-none", "[-webkit-touch-callout:none]");
    expect(header).not.toHaveClass("md:select-none", "md:[-webkit-touch-callout:none]");
  });

  it("keeps the row highlighted while a menu is open", () => {
    // A right-click opens the context menu in a portal, so :hover drops off the
    // header — the row stays highlighted off the open menu's `data-state` instead.
    renderSidebar();

    expect(folderHeader()).toHaveClass(
      "group-has-[[data-state=open]]/header:bg-muted",
      "group-has-[[data-state=open]]/header:text-foreground",
    );
  });

  it("opens after a stationary touch long-press", () => {
    vi.useFakeTimers();
    try {
      renderSidebar();
      const header = folderHeader();
      fireEvent.click(header);
      const before = header.getAttribute("aria-expanded");
      if (before === null) throw new Error("Folder header is missing aria-expanded");

      dispatchTouchPointer(header, "pointerdown");
      act(() => vi.advanceTimersByTime(699));
      expect(screen.queryByTestId("rename-project")).toBeNull();
      act(() => vi.advanceTimersByTime(1));

      // Long-press exposes the complete action set on touch.
      expectProjectMenuActions();
      expect(document.body).toHaveStyle({ pointerEvents: "none" });
      dispatchTouchPointer(header, "pointerup");
      expect(header).toHaveAttribute("aria-expanded", before);
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it("cancels a touch long-press when the pointer moves", () => {
    vi.useFakeTimers();
    try {
      renderSidebar();
      const header = folderHeader();

      // Prove the same touch path can open before exercising cancellation.
      dispatchTouchPointer(header, "pointerdown");
      act(() => vi.advanceTimersByTime(700));
      expect(screen.getByTestId("rename-project")).toBeInTheDocument();
      dismissMenu();

      dispatchTouchPointer(header, "pointerdown");
      dispatchTouchPointer(header, "pointermove");
      act(() => vi.advanceTimersByTime(700));

      expect(screen.queryByTestId("rename-project")).toBeNull();
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it("opens for a keyboard-originated contextmenu event", () => {
    renderSidebar();
    const header = folderHeader();
    header.focus();

    fireEvent.contextMenu(header, { detail: 0 });
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
  });

  it("toggles on the first keyboard activation after dismissal", async () => {
    renderSidebar();
    const header = folderHeader();
    header.focus();

    fireEvent.contextMenu(header, { detail: 0 });
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
    dismissMenu();
    await waitFor(() => expect(header).toHaveFocus());
    fireEvent.click(header, { detail: 0 });

    expect(folderHeader()).toHaveAttribute("aria-expanded", "true");
  });

  it("scopes the trigger to the header button", () => {
    renderSidebar();

    fireEvent.click(folderHeader());
    const row = screen.getByRole("link", { name: /My Session/ });
    const trigger = contextTrigger();

    expect(trigger).toBe(folderHeader());
    expect(trigger.contains(row)).toBe(false);
  });

  it("leaves a nested session row's context menu intact", () => {
    renderSidebar();

    fireEvent.click(folderHeader());
    const row = screen.getByRole("link", { name: /My Session/ });
    expect(contextTrigger().contains(row)).toBe(false);
    fireEvent.contextMenu(row);

    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("archive-conversation")).toBeInTheDocument();
    expect(screen.queryByTestId("rename-project")).toBeNull();
  });

  it("suppresses the context menu in bulk-selection mode", () => {
    renderSidebar();

    expect(contextTrigger()).toBe(folderHeader());
    fireEvent.click(folderHeader());
    fireEvent.pointerDown(screen.getByTestId("project-list-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("projects-select-sessions"));

    expect(folderHeader().closest('[data-slot="context-menu-trigger"]')).toBeNull();
    expect(folderHeader()).toHaveClass("select-none", "[-webkit-touch-callout:none]");
    fireEvent.contextMenu(folderHeader());
    expect(screen.queryByTestId("rename-project")).toBeNull();
  });

  it("keeps the collapse toggle active when selection mode unmounts an open menu", () => {
    renderSidebar();
    const header = folderHeader();
    const before = header.getAttribute("aria-expanded");
    if (before === null) throw new Error("Folder header is missing aria-expanded");

    fireEvent.pointerDown(screen.getByTestId("project-list-actions"), { button: 0 });
    fireEvent.contextMenu(header);
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("projects-select-sessions"));
    const selectionHeader = folderHeader();
    expect(selectionHeader.closest('[data-slot="context-menu-trigger"]')).toBeNull();
    fireEvent.click(selectionHeader);

    expect(selectionHeader).toHaveAttribute("aria-expanded", before === "true" ? "false" : "true");
  });
});
