import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { Conversation } from "@/hooks/useConversations";
import type * as ConversationsModule from "@/hooks/useConversations";
import type * as UnseenConversationsModule from "@/hooks/useUnseenConversations";
import { setOmnigentHostConfig } from "@/lib/host";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { HeaderConversationMenu } from "./HeaderConversationMenu";

const mocks = vi.hoisted(() => ({
  isMobile: false,
  projects: [{ id: "project-1", name: "Sprint 42" }] as {
    id: string | null;
    name: string;
    icon?: string | null;
  }[],
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

const SECOND_CONVERSATION: Conversation = {
  ...CONVERSATION,
  id: "conv-2",
  title: "Release planning",
  updated_at: 1_700_000_200,
  git_branch: "feature/release-planning",
};

function menuTree(overrides: Partial<Parameters<typeof HeaderConversationMenu>[0]> = {}) {
  return (
    <MemoryRouter initialEntries={[`/c/${overrides.conversation?.id ?? CONVERSATION.id}`]}>
      <HeaderConversationMenu
        conversation={CONVERSATION}
        currentProject={null}
        canShare
        onShare={() => {}}
        {...overrides}
      />
    </MemoryRouter>
  );
}

function renderMenu(overrides: Partial<Parameters<typeof HeaderConversationMenu>[0]> = {}) {
  return render(menuTree(overrides));
}

function openMenu() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
    button: 0,
  });
}

beforeEach(() => {
  setOmnigentHostConfig({});
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
    expect(trigger).not.toHaveClass("md:opacity-0");
    expect(trigger).not.toHaveClass("md:group-hover/breadcrumb:opacity-100");
    expect(trigger).not.toHaveClass("md:group-focus-within/breadcrumb:opacity-100");
    expect(trigger).not.toHaveClass("data-[state=open]:opacity-100");
    expect(trigger.querySelector("svg")).toHaveClass("lucide-ellipsis");

    openMenu();
    // Rename and Move session live in the kebab on desktop too, alongside the
    // breadcrumb title (HeaderTitle) and folder tag (HeaderProjectTag) shortcuts.
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent?.trim())).toEqual([
      "Pin",
      "Share",
      "Rename",
      "Mark as unread",
      "Add to project",
      "Archive",
      "Delete",
    ]);
    expect(screen.getByTestId("header-move-to-project")).toBeInTheDocument();
    expect(screen.getByTestId("header-rename-conversation")).toBeInTheDocument();
  });

  it("opens from the keyboard and focuses the first action", () => {
    renderMenu();
    const trigger = screen.getByRole("button", { name: "Conversation actions" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });

    expect(screen.getByRole("menuitem", { name: "Pin" })).toHaveFocus();
  });

  it("runs pin and mark-unread actions for the active session", () => {
    renderMenu();

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Pin" }));
    expect(mocks.togglePinned).toHaveBeenCalledWith({ id: "conv-1", pinned: true });

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Mark as unread" }));
    expect(mocks.markUnread).toHaveBeenCalledWith("conv-1", 1_700_000_100);
  });

  it("renames from the mobile Rename dialog", () => {
    // Rename is mobile-only in this menu now — desktop renames by clicking the
    // breadcrumb title (HeaderTitle).
    mocks.isMobile = true;
    renderMenu();

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: "Session name" });
    expect(input).toHaveAttribute("maxLength", String(USER_SESSION_TITLE_MAX_CHARS));
    fireEvent.change(input, { target: { value: "Roadmap planning" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    expect(mocks.rename).toHaveBeenCalledWith({ id: "conv-1", title: "Roadmap planning" });
  });

  it("runs archive and delete actions for the active session", () => {
    const view = renderMenu();

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Archive" }));
    // Just the flag: the optimistic overlay lives in the hook, and the
    // "view archived" toast fires synchronously (navigating away unmounts this
    // menu, so a mutate onSuccess callback wouldn't fire).
    expect(mocks.archive).toHaveBeenCalledWith({ id: "conv-1", archived: true });

    view.unmount();
    renderMenu();
    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    fireEvent.click(screen.getByTestId("header-delete-branch-checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mocks.deleteConversation).toHaveBeenCalledWith({
      id: "conv-1",
      deleteBranch: true,
    });
  });

  it("labels project actions for filed and unfiled sessions", () => {
    // Move to project is mobile-only in this menu now (desktop moved it to the
    // breadcrumb folder tag).
    mocks.isMobile = true;
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
    expect(screen.getByRole("textbox", { name: "Search or create project" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Sprint 42" }));
    expect(mocks.moveToProject).toHaveBeenCalledWith({ id: "conv-1", project: "Sprint 42" });
  });

  it("opens the project picker as a submenu flyout on desktop and moves the session", async () => {
    // Desktop has room for a side flyout, so Move session is a submenu trigger
    // (not the in-place body swap the mobile menu uses).
    renderMenu();
    openMenu();

    const projectAction = screen.getByTestId("header-move-to-project");
    expect(projectAction).toHaveAttribute("aria-haspopup", "menu");
    // No in-place "Back" affordance — the flyout keeps the parent items visible.
    expect(screen.queryByTestId("header-project-picker-back")).toBeNull();

    fireEvent.click(projectAction);
    expect(
      await screen.findByRole("textbox", { name: "Search or create project" }),
    ).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Sprint 42" }));
    expect(mocks.moveToProject).toHaveBeenCalledWith({ id: "conv-1", project: "Sprint 42" });
  });

  it("orders decorative project icons and a folder fallback before their names", () => {
    mocks.isMobile = true;
    mocks.projects = [
      { id: "project-1", name: "Sprint 42", icon: "🚀" },
      { id: null, name: "Legacy project" },
    ];
    renderMenu({ currentProject: "Sprint 42" });
    openMenu();
    fireEvent.click(screen.getByTestId("header-move-to-project"));

    const iconRow = screen.getByRole("menuitem", { name: "Sprint 42" });
    const emoji = iconRow.firstElementChild;
    expect(emoji).toHaveAttribute("data-testid", "project-icon");
    expect(emoji).toHaveAttribute("aria-hidden", "true");
    expect(emoji).toHaveTextContent("🚀");
    expect(emoji?.nextElementSibling).toHaveTextContent("Sprint 42");

    const fallback = screen.getByRole("menuitem", { name: "Legacy project" }).firstElementChild;
    expect(fallback?.tagName.toLowerCase()).toBe("svg");
    expect(fallback).toHaveAttribute("aria-hidden", "true");
    expect(fallback?.nextElementSibling).toHaveTextContent("Legacy project");

    const removeItem = screen.getByRole("menuitem", { name: "Remove from Sprint 42" });
    expect(removeItem.firstElementChild).toHaveAttribute("data-testid", "project-icon");
    expect(removeItem.firstElementChild).toHaveTextContent("🚀");
  });

  it("uses project names and the Remove label for the mobile picker typeahead", async () => {
    mocks.isMobile = true;
    mocks.projects = [
      { id: null, name: "Alpha" },
      { id: "project-1", name: "Sprint 42", icon: "🚀" },
    ];
    const view = renderMenu({ currentProject: "Sprint 42" });
    openMenu();
    fireEvent.click(screen.getByTestId("header-move-to-project"));

    const alphaRow = await screen.findByRole("menuitem", { name: "Alpha" });
    const sprintRow = screen.getByRole("menuitem", { name: "Sprint 42" });
    expect(alphaRow.firstElementChild?.tagName.toLowerCase()).toBe("svg");
    expect(alphaRow.firstElementChild).toHaveAttribute("aria-hidden", "true");
    expect(alphaRow.firstElementChild?.nextElementSibling).toHaveTextContent("Alpha");
    expect(sprintRow.firstElementChild).toHaveAttribute("data-testid", "project-icon");
    expect(sprintRow.firstElementChild?.nextElementSibling).toHaveTextContent("Sprint 42");
    alphaRow.focus();
    fireEvent.keyDown(alphaRow, { key: "s" });
    await waitFor(() => expect(sprintRow).toHaveFocus());

    // A fresh mount resets the typeahead buffer so "r" matches the Remove row
    // instead of extending the previous "s" search.
    view.unmount();
    renderMenu({ currentProject: "Sprint 42" });
    openMenu();
    fireEvent.click(screen.getByTestId("header-move-to-project"));

    const freshAlphaRow = await screen.findByRole("menuitem", { name: "Alpha" });
    const removeItem = screen.getByRole("menuitem", { name: "Remove from Sprint 42" });
    freshAlphaRow.focus();
    fireEvent.keyDown(freshAlphaRow, { key: "r" });
    await waitFor(() => expect(removeItem).toHaveFocus());
  });

  it("closes and resets Rename when the conversation id changes", async () => {
    // Rename item is mobile-only now.
    mocks.isMobile = true;
    const view = renderMenu();
    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Session name" }), {
      target: { value: "Stale session A title" },
    });

    view.rerender(menuTree({ conversation: SECOND_CONVERSATION }));

    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: "Rename session" })).toBeNull();
    });
    expect(mocks.rename).not.toHaveBeenCalled();

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: "Session name" });
    expect(input).toHaveValue("Release planning");
    fireEvent.change(input, { target: { value: "Release launch" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    expect(mocks.rename).toHaveBeenCalledWith({ id: "conv-2", title: "Release launch" });
  });

  it("closes Delete and clears branch selection when the conversation id changes", async () => {
    const view = renderMenu();
    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    fireEvent.click(screen.getByTestId("header-delete-branch-checkbox"));

    view.rerender(menuTree({ conversation: SECOND_CONVERSATION }));

    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: "Delete conversation?" })).toBeNull();
    });
    expect(mocks.deleteConversation).not.toHaveBeenCalled();

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(screen.getByTestId("header-delete-branch-checkbox")).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mocks.deleteConversation).toHaveBeenCalledWith({
      id: "conv-2",
      deleteBranch: false,
    });
  });

  it("heads the mobile menu with the session title and drops it in the picker", () => {
    // The mobile chat header shows no title of its own (the native shells hide
    // the breadcrumb), so the menu itself has to name the session.
    mocks.isMobile = true;
    renderMenu();
    openMenu();
    const label = screen.getByText("Quarterly planning");
    expect(label).toHaveAttribute("data-slot", "dropdown-menu-label");

    fireEvent.click(screen.getByTestId("header-move-to-project"));
    expect(screen.queryByText("Quarterly planning")).toBeNull();
  });

  it("omits the title header on desktop, where the breadcrumb already shows it", () => {
    renderMenu();
    openMenu();

    expect(screen.queryByText("Quarterly planning")).toBeNull();
  });

  it("sizes the trigger and rows for touch on mobile only", () => {
    const view = renderMenu();
    const desktopTrigger = screen.getByRole("button", { name: "Conversation actions" });
    expect(desktopTrigger.querySelector("svg")).toHaveClass("size-3.5");
    openMenu();
    expect(screen.getByRole("menuitem", { name: "Pin" })).not.toHaveClass("py-2");

    view.unmount();
    mocks.isMobile = true;
    renderMenu();
    const mobileTrigger = screen.getByRole("button", { name: "Conversation actions" });
    expect(mobileTrigger.querySelector("svg")).toHaveClass("size-4");
    openMenu();
    expect(screen.getByRole("menuitem", { name: "Pin" })).toHaveClass("gap-2.5", "px-2.5", "py-2");
  });

  it("keeps every session action reachable on mobile", () => {
    // Regression guard for the native mobile shells, where this menu is the
    // only entry point to the session operations.
    mocks.isMobile = true;
    renderMenu();
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

  it("slots workspace entries between the session actions and the destructive block", () => {
    // On mobile these are the rail drawers folded into this same menu; Archive
    // and Delete must stay last so a mis-tap doesn't land on them.
    mocks.isMobile = true;
    renderMenu({
      workspaceItems: <DropdownMenuItem data-testid="rail-files">Files</DropdownMenuItem>,
    });
    openMenu();

    expect(screen.getAllByRole("menuitem").map((item) => item.textContent?.trim())).toEqual([
      "Pin",
      "Share",
      "Rename",
      "Mark as unread",
      "Add to project",
      "Files",
      "Archive",
      "Delete",
    ]);
  });

  it("renders no workspace section when there are no entries", () => {
    const view = renderMenu();
    openMenu();
    const baseSeparators = screen.getAllByRole("separator").length;

    view.unmount();
    renderMenu({
      workspaceItems: <DropdownMenuItem data-testid="rail-files">Files</DropdownMenuItem>,
    });
    openMenu();
    // The entries bring exactly one separator of their own.
    expect(screen.getAllByRole("separator")).toHaveLength(baseSeparators + 1);
  });

  it("keeps the page touchable while the mobile menu is open (non-modal)", () => {
    // Modal mode sets pointer-events:none on <body>, leaving the menu as the
    // only touch target; browser touch-target adjustment then snaps outside
    // taps onto the menu, so on a phone it can never be dismissed. Mobile must
    // run non-modal so an outside tap lands on real content and closes it.
    mocks.isMobile = true;
    renderMenu();
    openMenu();

    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(document.body.style.pointerEvents).not.toBe("none");
  });

  it("stays modal on desktop, where outside clicks are precise", () => {
    renderMenu();
    openMenu();

    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(document.body.style.pointerEvents).toBe("none");
  });

  it("wears the mobile glass surface and a round trigger", () => {
    // The trigger sits inside the header's round floating pill; a rounded-lg
    // open-state background showed through it as a square.
    mocks.isMobile = true;
    renderMenu();
    const trigger = screen.getByRole("button", { name: "Conversation actions" });
    expect(trigger).toHaveClass("max-md:rounded-full");

    openMenu();
    expect(screen.getByRole("menu")).toHaveClass(
      "max-md:bg-background/70",
      "max-md:backdrop-blur-xl",
    );
  });

  it("keeps emitting the mobile Share / Agent info analytics ids", () => {
    // These two actions moved here from the header's legacy Share · Agent info
    // menu, which reported them under these ids. An owner-managed session no
    // longer renders that menu, so this path has to carry the series forward.
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    mocks.isMobile = true;
    const onShare = vi.fn();
    const onAgentInfo = vi.fn();
    renderMenu({ onShare, hasAgentInfo: true, onAgentInfo });

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Share" }));
    expect(analytics).toHaveBeenCalledWith({
      type: "click",
      componentId: "chat.header.mobile_share",
      componentKind: "button",
    });
    expect(onShare).toHaveBeenCalledOnce();

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Agent info" }));
    expect(analytics).toHaveBeenCalledWith({
      type: "click",
      componentId: "chat.header.mobile_agent_info",
      componentKind: "button",
    });
    expect(onAgentInfo).toHaveBeenCalledOnce();
  });

  it("reports nothing for the desktop kebab, a different surface", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    renderMenu({ onShare: () => {} });

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Share" }));
    expect(analytics).not.toHaveBeenCalled();
  });

  it("emits nothing when a disabled Share is selected", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    mocks.isMobile = true;
    const onShare = vi.fn();
    renderMenu({ onShare, shareDisabled: true });

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Share" }));
    expect(analytics).not.toHaveBeenCalled();
    expect(onShare).not.toHaveBeenCalled();
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
