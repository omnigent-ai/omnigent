// Tests for the project-folder header's right-click / long-press context menu.
//
// Project folder headers historically had only a hover-revealed kebab: a
// right-click fell through to the browser's native menu, and touch (which has
// no right-click) had no way to reach the actions at all. The header button now
// also carries a Radix `ContextMenuTrigger`, which supplies BOTH gestures —
// `contextmenu` for mouse and a built-in 700ms pointerdown timer for touch — so
// the same actions are reachable either way. The kebab stays.
//
// The menu body is authored once (`ProjectFolderMenuItems`) and rendered under
// both primitive families via the dropdown/context `MenuComponents` bundles, the
// same pattern the session rows use (`ConversationMenuItems`).
//
// What's locked in here:
//   1. Parity — the context menu carries the kebab's exact items, and each one
//      opens the same dialog as the kebab's.
//   2. Left-click still expands/collapses the folder.
//   3. Opening the context menu does NOT toggle expansion (the pre-existing
//      trap: the header button's onClick flips expand/collapse, and a
//      long-press's trailing click would otherwise fire it).
//   4. A nested session row keeps its OWN context menu — the trigger wraps the
//      header button, not the section, so right-click isn't hijacked.
//   5. Bulk-selection mode suppresses the header context menu, matching rows.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

// `isMobile` drives the mocked `useIsMobileViewport` (jsdom doesn't evaluate
// media queries), so the mobile-only "New session" item can be exercised.
const mocks = vi.hoisted(() => ({
  isMobile: false,
  renameProject: { mutate: vi.fn() },
  deleteProject: { mutate: vi.fn() },
}));

vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => mocks.isMobile,
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
    variables: undefined,
  }),
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [{ id: PROJECT_ID, name: PROJECT_NAME }] }),
  // The folder sources its members from the globally-loaded window too, so the
  // nested-row test doesn't need this query to return anything.
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
  useDeleteProject: () => ({ ...mocks.deleteProject, isPending: false, isError: false }),
  useRenameProject: () => ({ ...mocks.renameProject, isPending: false, isError: false }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useProjectConfig: () => ({ data: undefined, isLoading: false }),
  useUpdateProjectConfig: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("./AgentTypeFilter", () => ({ AgentTypeFilter: () => null }));
vi.mock("./ReportIssueButton", () => ({ ReportIssueButton: () => null }));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const PROJECT_NAME = "Sprint 42";
const PROJECT_ID = "p_sprint42";

const useConvMock = vi.mocked(useConversations);

/** A session filed under PROJECT_NAME, so the folder holds a nested row. */
const FILED_CONV: Conversation = {
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
  useConvMock.mockImplementation(() => result);
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

/** The project folder's collapse-toggle header button. An open Radix menu is
 *  modal and `aria-hidden`s the rest of the tree, so role queries can't see the
 *  header while the menu is up — match on the accessible name via the DOM. */
function folderHeader(): HTMLElement {
  const header = Array.from(document.querySelectorAll("h2 button")).find(
    (b) => b.textContent === PROJECT_NAME,
  );
  if (header === undefined) throw new Error(`no folder header for ${PROJECT_NAME}`);
  return header as HTMLElement;
}

/** With `asChild`, Radix's ContextMenuTrigger renders no wrapper — its props
 *  (data-slot, the long-press pointer handlers) merge onto the header button
 *  itself, so `closest` matches the button and this equals folderHeader(). */
function contextTrigger(): HTMLElement {
  const trigger = folderHeader().closest('[data-slot="context-menu-trigger"]');
  if (trigger === null) throw new Error("folder header has no context-menu trigger");
  return trigger as HTMLElement;
}

/** Simulate a touch long-press on the folder header: Radix arms a 700ms timer
 *  on a non-mouse pointerdown, so advance fake timers past it. */
function longPressHeader() {
  fireEvent.pointerDown(contextTrigger(), { pointerType: "touch", button: 0 });
  // The open happens inside the timer callback, so flush it under act().
  act(() => {
    vi.advanceTimersByTime(750);
  });
}

/** Dismiss the open menu with Escape, aimed at whatever the menu focused. */
function dismissMenu() {
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
}

beforeEach(() => {
  mocks.isMobile = false;
  mocks.renameProject.mutate.mockReset();
  mocks.deleteProject.mutate.mockReset();
  useConvMock.mockReset();
  localStorage.clear();
  mockConversations([FILED_CONV]);
});

afterEach(cleanup);

describe("project folder header context menu", () => {
  it("opens the kebab's exact action set on right-click", () => {
    renderSidebar();

    // Nothing rendered until the header is right-clicked (the kebab is closed).
    expect(screen.queryByTestId("rename-project")).toBeNull();

    fireEvent.contextMenu(folderHeader());

    // Same testids as the kebab body — it renders from the shared
    // ProjectFolderMenuItems, so the two menus can't diverge.
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
    expect(screen.getByTestId("project-settings")).toBeInTheDocument();
    expect(screen.getByTestId("delete-project")).toBeInTheDocument();
    // "New session" is present but mobile-only (md:hidden), matching the kebab.
    expect(screen.getByTestId("project-new-session-menu")).toHaveClass("md:hidden");
  });

  it("carries exactly the same items as the kebab", () => {
    // Parity asserted item-for-item rather than by a hand-written list, so a
    // future kebab item that skips the shared body fails here.
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("project-actions"), { button: 0 });
    const kebabItems = screen
      .getAllByRole("menuitem")
      .map((el) => el.getAttribute("data-testid"))
      .filter((id): id is string => id !== null);
    expect(kebabItems.length).toBeGreaterThan(0);

    cleanup();
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    const contextItems = screen
      .getAllByRole("menuitem")
      .map((el) => el.getAttribute("data-testid"))
      .filter((id): id is string => id !== null);

    expect(contextItems).toEqual(kebabItems);
  });

  it("keeps the kebab working unchanged", () => {
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("project-actions"), { button: 0 });

    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("rename-project"));
    expect(screen.getByTestId("rename-project-confirm")).toBeInTheDocument();
  });

  it("drives Rename from the context menu into the same dialog and mutation", () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("rename-project"));

    // The shared rename dialog — same testid the kebab path opens.
    const confirm = screen.getByTestId("rename-project-confirm");
    expect(confirm).toBeInTheDocument();

    const input = screen.getByDisplayValue(PROJECT_NAME);
    fireEvent.change(input, { target: { value: "Sprint 43" } });
    fireEvent.click(confirm);

    expect(mocks.renameProject.mutate).toHaveBeenCalledWith(
      { id: PROJECT_ID, oldName: PROJECT_NAME, newName: "Sprint 43" },
      expect.anything(),
    );
  });

  it("drives Project settings from the context menu", () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("project-settings"));

    expect(screen.getByRole("dialog")).toHaveTextContent(/Project settings/i);
  });

  it("drives Delete from the context menu into the same confirm + mutation", () => {
    renderSidebar();

    fireEvent.contextMenu(folderHeader());
    fireEvent.click(screen.getByTestId("delete-project"));

    // The confirm dialog (delete archives every member, so it's gated).
    expect(screen.getByText("Delete project?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    expect(mocks.deleteProject.mutate).toHaveBeenCalledWith(
      { id: PROJECT_ID, name: PROJECT_NAME },
      expect.anything(),
    );
  });

  it("exposes the mobile-only New session item pre-filed under the project", () => {
    mocks.isMobile = true;
    renderSidebar();

    fireEvent.contextMenu(folderHeader());

    // asChild renders the item as the <a> itself, so the href lives on it.
    expect(screen.getByTestId("project-new-session-menu")).toHaveAttribute(
      "href",
      `/?project=${encodeURIComponent(PROJECT_NAME)}`,
    );
  });

  it("still expands and collapses the folder on plain left-click", () => {
    renderSidebar();

    // Project folders render collapsed by default.
    const header = folderHeader();
    expect(header).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(header);
    expect(folderHeader()).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(folderHeader());
    expect(folderHeader()).toHaveAttribute("aria-expanded", "false");
  });

  it("toggles on the first left-click after the mouse context menu is dismissed", () => {
    renderSidebar();

    expect(folderHeader()).toHaveAttribute("aria-expanded", "false");

    fireEvent.pointerDown(folderHeader(), { pointerType: "mouse", button: 2 });
    fireEvent.contextMenu(folderHeader());

    // The menu is open and the folder stayed collapsed — Radix preventDefaults
    // the native contextmenu, so no click reaches the collapse toggle.
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
    expect(folderHeader()).toHaveAttribute("aria-expanded", "false");

    dismissMenu();
    fireEvent.click(folderHeader());
    expect(folderHeader()).toHaveAttribute("aria-expanded", "true");
  });

  it("toggles on the first keyboard activation after its context menu is dismissed", async () => {
    renderSidebar();

    const header = folderHeader();
    header.focus();

    // jsdom does not synthesize contextmenu or click from keyboard keys, so
    // dispatch the browser-generated events explicitly without pointer events.
    fireEvent.keyDown(header, { key: "F10", shiftKey: true });
    fireEvent.contextMenu(header);
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();

    dismissMenu();
    await waitFor(() => expect(header).toHaveFocus());

    fireEvent.click(header, { detail: 0 });
    expect(folderHeader()).toHaveAttribute("aria-expanded", "true");
  });

  it("suppresses native text selection of the header title", () => {
    // A long-press must open the menu, not select the project name. Session
    // rows suppress selection by preventDefault-ing touch pointerdown, but on
    // this trigger that would also cancel Radix's composed long-press timer —
    // so the header relies on user-select: none instead. jsdom cannot run a
    // real selection, so pin the class that carries the behavior.
    renderSidebar();
    expect(folderHeader()).toHaveClass("select-none");
  });

  it("does not toggle the folder when a long-press opens the menu", () => {
    // The gotcha this PR had to solve: the long-press fires mid-gesture off
    // Radix's pointerdown timer, but the trailing pointerup still produces a
    // click — which would collapse/expand the folder under the just-opened menu.
    vi.useFakeTimers();
    try {
      renderSidebar();
      expect(folderHeader()).toHaveAttribute("aria-expanded", "false");

      longPressHeader();

      // The menu opened from touch alone — no custom gesture code, just Radix's
      // 700ms timer on the same trigger.
      expect(screen.getByTestId("rename-project")).toBeInTheDocument();

      // The trailing click of the press must NOT toggle the folder.
      fireEvent.pointerUp(contextTrigger(), { pointerType: "touch", button: 0 });
      fireEvent.click(folderHeader());
      expect(folderHeader()).toHaveAttribute("aria-expanded", "false");
    } finally {
      vi.useRealTimers();
    }
  });

  it("still toggles on a plain click after a long-press opened the menu", () => {
    // The click-swallow must be one-shot: a later ordinary click still expands.
    vi.useFakeTimers();
    try {
      renderSidebar();

      longPressHeader();
      fireEvent.pointerUp(contextTrigger(), { pointerType: "touch", button: 0 });
      fireEvent.click(folderHeader());
      expect(folderHeader()).toHaveAttribute("aria-expanded", "false");

      // A fresh, separate click (its own pointerdown) toggles as normal.
      fireEvent.pointerDown(folderHeader(), { pointerType: "mouse", button: 0 });
      fireEvent.click(folderHeader());
      expect(folderHeader()).toHaveAttribute("aria-expanded", "true");
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not swallow a keyboard toggle after a clickless non-mouse open", () => {
    // A pen barrel-button right-click (or a touch release over the portaled
    // menu) arms the swallow but never delivers a trailing click. Closing the
    // menu must disarm it, or the next keyboard Enter is silently eaten.
    renderSidebar();
    expect(folderHeader()).toHaveAttribute("aria-expanded", "false");

    fireEvent.pointerDown(contextTrigger(), { pointerType: "pen", button: 2 });
    fireEvent.contextMenu(folderHeader());
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();

    dismissMenu();

    // Keyboard activation (click with no preceding pointerdown) still toggles.
    fireEvent.click(folderHeader(), { detail: 0 });
    expect(folderHeader()).toHaveAttribute("aria-expanded", "true");
  });

  it("scopes the header trigger to the header button, clear of the nested rows", () => {
    // Structural guard on placement. The trigger must wrap the header BUTTON,
    // never an ancestor that also contains the child rows (the outer folder
    // div, or the <section>) — those would put every nested session row inside
    // the project's trigger and hijack right-click on them.
    renderSidebar();

    // Expand the folder so its member row renders inside the section.
    fireEvent.click(folderHeader());
    const row = screen.getByRole("link", { name: /My Session/ });

    const trigger = contextTrigger();
    expect(trigger.contains(folderHeader())).toBe(true);
    expect(trigger.contains(row)).toBe(false);
  });

  it("leaves a nested session row's own context menu intact", () => {
    renderSidebar();

    fireEvent.click(folderHeader());
    const row = screen.getByRole("link", { name: /My Session/ });

    fireEvent.contextMenu(row);

    // The SESSION's menu opened (row actions), not the project's.
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("archive-conversation")).toBeInTheDocument();
    expect(screen.queryByTestId("rename-project")).toBeNull();
    expect(screen.queryByTestId("delete-project")).toBeNull();
  });

  it("suppresses the header context menu in bulk-selection mode", () => {
    // Selection mode owns the rows, and the session rows already drop their
    // context menus there — the folder header matches that.
    renderSidebar();

    fireEvent.click(folderHeader());
    fireEvent.pointerDown(screen.getByTestId("project-list-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("projects-select-sessions"));

    // In selection mode the header no longer carries a context-menu trigger,
    // and right-clicking it opens nothing.
    expect(folderHeader().closest('[data-slot="context-menu-trigger"]')).toBeNull();
    fireEvent.contextMenu(folderHeader());
    expect(screen.queryByTestId("rename-project")).toBeNull();
  });
});
