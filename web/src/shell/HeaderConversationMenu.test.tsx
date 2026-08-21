import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { Conversation } from "@/hooks/useConversations";
import type * as ConversationsModule from "@/hooks/useConversations";
import type * as UnseenConversationsModule from "@/hooks/useUnseenConversations";
import { HeaderConversationMenu } from "./HeaderConversationMenu";

const mocks = vi.hoisted(() => ({
  isMobile: false,
  projects: [{ id: "project-1", name: "Sprint 42" }],
  togglePinned: vi.fn(),
  rename: vi.fn(),
  moveToProject: vi.fn(),
  archive: vi.fn(),
  deleteConversation: vi.fn(),
  markUnread: vi.fn(),
}));

vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => mocks.isMobile,
}));

vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  return {
    ...actual,
    useProjects: () => ({ data: mocks.projects }),
    useTogglePinnedConversation: () => ({ mutate: mocks.togglePinned }),
    useRenameConversation: () => ({ mutate: mocks.rename, isPending: false }),
    useMoveToProject: () => ({ mutate: mocks.moveToProject }),
    useArchiveConversation: () => ({ mutate: mocks.archive }),
    useStopAndDeleteConversation: () => ({
      mutate: mocks.deleteConversation,
      isPending: false,
    }),
  };
});

vi.mock("@/hooks/useUnseenConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof UnseenConversationsModule>();
  return { ...actual, markConversationUnread: mocks.markUnread };
});

const CONVERSATION: Conversation = {
  id: "conv-1",
  object: "conversation",
  title: "Quarterly planning",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_100,
  labels: {},
  permission_level: 3,
  git_branch: "feature/quarterly-planning",
};

function renderMenu(overrides: Partial<Parameters<typeof HeaderConversationMenu>[0]> = {}) {
  return render(
    <MemoryRouter initialEntries={["/c/conv-1"]}>
      <HeaderConversationMenu
        conversation={CONVERSATION}
        currentProject={null}
        canShare
        onShare={() => {}}
        {...overrides}
      />
    </MemoryRouter>,
  );
}

function openMenu() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
    button: 0,
  });
}

beforeEach(() => {
  mocks.isMobile = false;
  mocks.projects = [{ id: "project-1", name: "Sprint 42" }];
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("HeaderConversationMenu", () => {
  it("exposes an accessible trigger and the established action order", () => {
    renderMenu();
    const trigger = screen.getByRole("button", { name: "Conversation actions" });
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");

    openMenu();
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent?.trim())).toEqual([
      "Pin",
      "Share",
      "Rename",
      "Mark as unread",
      "Add to project",
      "Archive",
      "Delete",
    ]);
  });

  it("opens from the keyboard and focuses the first action", () => {
    renderMenu();
    const trigger = screen.getByRole("button", { name: "Conversation actions" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });

    expect(screen.getByRole("menuitem", { name: "Pin" })).toHaveFocus();
  });

  it("runs pin, mark-unread, and rename actions for the active session", () => {
    renderMenu();

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Pin" }));
    expect(mocks.togglePinned).toHaveBeenCalledWith({ id: "conv-1", pinned: true });

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Mark as unread" }));
    expect(mocks.markUnread).toHaveBeenCalledWith("conv-1", 1_700_000_100);

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const input = screen.getByTestId("header-rename-conversation-input");
    fireEvent.change(input, { target: { value: "Roadmap planning" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    expect(mocks.rename).toHaveBeenCalledWith({ id: "conv-1", title: "Roadmap planning" });
  });

  it("labels project actions for filed and unfiled sessions", () => {
    const view = renderMenu();
    openMenu();
    expect(screen.getByTestId("header-move-to-project")).toHaveTextContent("Add to project");

    view.unmount();
    renderMenu({ currentProject: "Payments" });
    openMenu();
    expect(screen.getByTestId("header-move-to-project")).toHaveTextContent("Move session");
  });

  it("uses the in-place project picker on mobile and moves to the selected project", () => {
    mocks.isMobile = true;
    renderMenu();
    openMenu();

    const projectAction = screen.getByTestId("header-move-to-project");
    expect(projectAction).not.toHaveAttribute("aria-haspopup", "menu");
    fireEvent.click(projectAction);

    expect(screen.getByTestId("header-project-picker-back")).toBeInTheDocument();
    expect(screen.queryByTestId("header-rename-conversation")).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "Sprint 42" }));
    expect(mocks.moveToProject).toHaveBeenCalledWith({ id: "conv-1", project: "Sprint 42" });
  });

  it("reflects pinned state and preserves the disabled share reason", () => {
    renderMenu({
      conversation: {
        ...CONVERSATION,
        labels: { "omnigent.pinned": "1700000000000" },
      },
      shareDisabled: true,
      shareDisabledReason: "Sharing is unavailable from a local server.",
    });
    openMenu();

    expect(screen.getByRole("menuitem", { name: "Unpin" })).toBeInTheDocument();
    const share = screen.getByRole("menuitem", { name: "Share" });
    expect(share).toHaveAttribute("data-disabled");
    expect(share).toHaveAttribute("title", "Sharing is unavailable from a local server.");
  });
});
