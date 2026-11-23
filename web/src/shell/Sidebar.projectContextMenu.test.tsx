import { EXPANDED_PROJECT_SECTIONS_STORAGE_KEY } from "@/shell/sidebarNav";
import { conversationPage, setConversationMutedMock } from "@/test/sidebarMockHelpers";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

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
  updateProjectConfig: {
    mutate: vi.fn(),
    mutateAsync: vi.fn(() => Promise.resolve()),
    isPending: false,
    isError: false,
    error: null,
  },
  projectConfig: { host_id: "h1" } as Record<string, unknown>,
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [] }),
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
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
    useDeleteProject: () => mocks.deleteProject,
    useRenameProject: () => mocks.renameProject,
    useProjectConfig: () => ({ data: mocks.projectConfig, isLoading: false, isError: false }),
    useUpdateProjectConfig: () => mocks.updateProjectConfig,
  };
});

vi.mock("./ProjectSettingsDialog", () => ({
  ProjectSettingsDialog: ({ open }: { open: boolean }) =>
    open ? <div role="dialog">Project settings</div> : null,
}));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { clearSessionMuteState, MUTED_LABEL_KEY, setMutedProjects } from "@/lib/sessionMute";

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
  const result = conversationPage(conversations);
  useConversationsMock.mockImplementation(() => result);
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
  mocks.projectConfig = { host_id: "h1" };
  useConversationsMock.mockReset();
  localStorage.clear();
  mockConversations([FILED_CONVERSATION]);
});

afterEach(() => {
  cleanup();
  act(() => clearSessionMuteState());
});

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

  it("focuses and selects the project name when the rename dialog opens", async () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("rename-project"));

    const input = screen.getByDisplayValue(PROJECT_NAME) as HTMLInputElement;
    await waitFor(() => expect(input).toHaveFocus());
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(PROJECT_NAME.length);
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

  it("keeps an expanded folder expanded under its new name", async () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify([PROJECT_NAME]));
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("rename-project"));
    fireEvent.change(screen.getByDisplayValue(PROJECT_NAME), {
      target: { value: "Sprint 43" },
    });
    fireEvent.click(screen.getByTestId("rename-project-confirm"));

    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY)!)).toEqual([
        "Sprint 43",
      ]),
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

  it("asks again before Delete stops sessions whose agents are working", () => {
    interface DeleteCallbacks {
      onSuccess?: (result: unknown) => void;
    }
    mocks.deleteProject.mutate.mockImplementationOnce((_vars: unknown, opts: DeleteCallbacks) =>
      opts.onSuccess?.({ status: "needs_running_confirmation", runningCount: 2 }),
    );
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("delete-project"));
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    expect(screen.getByTestId("delete-project-running-warning")).toHaveTextContent(
      `2 sessions in ${PROJECT_NAME} have agents still working.`,
    );
    expect(mocks.deleteProject.mutate).toHaveBeenLastCalledWith(
      { id: PROJECT_ID, name: PROJECT_NAME },
      expect.anything(),
    );

    mocks.deleteProject.mutate.mockImplementationOnce((_vars: unknown, opts: DeleteCallbacks) =>
      opts.onSuccess?.({ status: "deleted", succeeded: [], failed: [] }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete anyway" }));

    expect(mocks.deleteProject.mutate).toHaveBeenLastCalledWith(
      { id: PROJECT_ID, name: PROJECT_NAME, stopRunning: true },
      expect.anything(),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    // Reopening starts from the plain confirmation, not the earlier warning.
    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("delete-project"));
    expect(screen.queryByTestId("delete-project-running-warning")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete project" })).toBeInTheDocument();
  });

  it("drops the working-agent warning when Delete is cancelled and reopened", () => {
    mocks.deleteProject.mutate.mockImplementationOnce(
      (_vars: unknown, opts: { onSuccess?: (result: unknown) => void }) =>
        opts.onSuccess?.({ status: "needs_running_confirmation", runningCount: 1 }),
    );
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("delete-project"));
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));
    expect(screen.getByTestId("delete-project-running-warning")).toHaveTextContent(
      `1 session in ${PROJECT_NAME} has an agent still working.`,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("delete-project"));
    expect(screen.queryByTestId("delete-project-running-warning")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    expect(mocks.deleteProject.mutate).toHaveBeenCalledTimes(2);
    expect(mocks.deleteProject.mutate).toHaveBeenLastCalledWith(
      { id: PROJECT_ID, name: PROJECT_NAME },
      expect.anything(),
    );
  });

  it("keeps New session reachable when a touch opens the menu on hover-capable hardware", () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    const item = screen.getByTestId("project-new-session-menu");

    expect(item).toHaveAttribute("href", `/?project=${encodeURIComponent(PROJECT_NAME)}`);
    for (const hiddenClass of ["hidden", "md:hidden", "md:hidden"]) {
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

  it("restores keyboard focus on dismissal after a previous menu opened a dialog", async () => {
    renderSidebar();
    const header = folderHeader();
    fireEvent.contextMenu(header);
    fireEvent.click(screen.getByTestId("rename-project"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
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

describe("project mute", () => {
  const MUTE_SERVER = { ...FALLBACK_SERVER_INFO, session_mute: true, project_mute: true };

  it("mutes the project by merging onto its stored config", () => {
    renderSidebar({ info: MUTE_SERVER });
    expect(screen.queryByTestId("project-muted-icon")).toBeNull();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByRole("menuitem", { name: "Mute project" }));

    expect(mocks.updateProjectConfig.mutate).toHaveBeenCalledWith(
      { id: PROJECT_ID, name: PROJECT_NAME, config: { host_id: "h1", muted: true } },
      expect.anything(),
    );
  });

  it("marks a muted project and unmutes it from the same menu", () => {
    act(() => setMutedProjects([{ id: PROJECT_ID, name: PROJECT_NAME, muted: true }], null));
    mocks.projectConfig = { host_id: "h1", muted: true };
    renderSidebar({ info: MUTE_SERVER });
    expect(screen.getByTestId("project-muted-icon")).toBeInTheDocument();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByRole("menuitem", { name: "Unmute project" }));

    expect(mocks.updateProjectConfig.mutate).toHaveBeenCalledWith(
      { id: PROJECT_ID, name: PROJECT_NAME, config: { host_id: "h1" } },
      expect.anything(),
    );
  });

  it.each([false, true])(
    "points a member session's own Mute at the project that muted it, own mute=%s",
    (ownMute) => {
      if (ownMute) {
        mockConversations([{ ...FILED_CONVERSATION, labels: { [MUTED_LABEL_KEY]: "1" } }]);
      }
      act(() => setMutedProjects([{ id: PROJECT_ID, name: PROJECT_NAME, muted: true }], null));
      localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify([PROJECT_NAME]));
      renderSidebar({ info: MUTE_SERVER });

      const row = screen.getByRole("link", { name: /My Session/ }).closest("li") as HTMLElement;
      expect(row.querySelector('[data-testid="conversation-muted-icon"]')).not.toBeNull();
      fireEvent.pointerDown(row.querySelector('[data-testid="conversation-actions"]')!, {
        button: 0,
      });
      // A session Unmute would leave the row muted, so the item says why instead.
      const item = screen.getByRole("menuitem", { name: "Muted with project" });
      expect(item).toHaveAttribute("data-disabled");
      expect(screen.queryByRole("menuitem", { name: "Unmute" })).toBeNull();
      // The session's own preference survives for when the project is unmuted.
      fireEvent.click(item);
      expect(setConversationMutedMock).not.toHaveBeenCalled();
    },
  );

  it("offers no project mute on a server that can't read it back", () => {
    renderSidebar({ info: { ...MUTE_SERVER, project_mute: false } });
    fireEvent.contextMenu(folderHeader());
    expect(screen.getByTestId("project-settings")).toBeInTheDocument();
    expect(screen.queryByTestId("mute-project")).toBeNull();
  });
});
