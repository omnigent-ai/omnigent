// Tests for the sidebar conversation-row quick actions:
//   1. A desktop quick pin/unpin button (`quick-pin-conversation`) and a
//      mobile-only kebab Pin item (`pin-conversation`) — two affordances for
//      the same pin toggle, split by viewport (responsive Tailwind classes).
//   2. Double-clicking a row to enter inline rename (ConversationRow's
//      `onDoubleClick`), gated on edit permission.
// See ConversationRow / ConversationEditRow in Sidebar.tsx.

import { Suspense, startTransition, useSyncExternalStore } from "react";
import type * as DndKitCore from "@dnd-kit/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { ServerInfo } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";

// Controllable rename mutation so the double-click test can assert the
// committed title was forwarded to the PATCH. The media-query hooks use
// separate flags so viewport layout and touch capability can vary independently.
// Declared via vi.hoisted so their mock factories can reference them.
const mocks = vi.hoisted(() => {
  // Tiny reactive store for the server-authoritative pinned set, so a quick-pin
  // click re-renders the sidebar (mirrors the real query's refetch). Holds ids;
  // the mocked hook maps them onto the loaded conversations.
  const pinnedListeners = new Set<() => void>();
  const pinnedStore = {
    ids: [] as string[],
    subscribe(cb: () => void) {
      pinnedListeners.add(cb);
      return () => pinnedListeners.delete(cb);
    },
    set(ids: string[]) {
      pinnedStore.ids = ids;
      pinnedListeners.forEach((cb) => cb());
    },
    toggle(id: string, pinned: boolean) {
      pinnedStore.set(
        pinned
          ? [id, ...pinnedStore.ids.filter((x) => x !== id)]
          : pinnedStore.ids.filter((x) => x !== id),
      );
    },
  };
  return {
    rename: { mutate: vi.fn() },
    isMobile: false,
    hasCoarsePointer: false,
    // Projects surfaced by the picker + the move-to-project mutation, so the
    // mobile in-place project view test can assert both the list and the pick.
    projects: [] as string[],
    moveToProject: { mutate: vi.fn() },
    conversations: [] as unknown[],
    isDragging: null as boolean | null,
    suspendDraggable: false,
    suspendedRender: new Promise<never>(() => {}),
    pinnedStore,
    // Archive + stop mutations, so the swipe tests can assert the swipe→archive
    // path drives the same stop→archive handler the kebab uses.
    archive: { mutate: vi.fn() },
    archiveOverride: null as null | { mutate: ReturnType<typeof vi.fn> },
    stopSession: { mutate: vi.fn() },
    // Stop-and-delete, so the swipe→delete test can assert the row deletes only
    // after the confirm dialog is accepted.
    del: { mutate: vi.fn(), reset: vi.fn() },
  };
});

vi.mock("@dnd-kit/core", async (importOriginal) => {
  const actual = await importOriginal<typeof DndKitCore>();
  return {
    ...actual,
    useDraggable: (args: Parameters<typeof actual.useDraggable>[0]) => {
      const draggable = actual.useDraggable(args);
      if (mocks.suspendDraggable) throw mocks.suspendedRender;
      return {
        ...draggable,
        isDragging: mocks.isDragging ?? draggable.isDragging,
      };
    },
  };
});

// Mock the mobile-viewport hook — jsdom doesn't evaluate media queries, so
// drive it explicitly. Defaults to desktop (false); the mobile flyout test
// flips `mocks.isMobile` for the duration of that case.
vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => mocks.isMobile,
}));

vi.mock("@/hooks/useCoarsePointer", () => ({
  useCoarsePointer: () => mocks.hasCoarsePointer,
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: mocks.del.mutate,
    reset: mocks.del.reset,
    isPending: false,
    isError: false,
    variables: undefined,
  }),
  // Reactive server pinned set: subscribes to the hoisted store so a toggle
  // re-renders, mapping pinned ids onto the loaded conversations.
  usePinnedConversations: () => {
    const ids = useSyncExternalStore(mocks.pinnedStore.subscribe, () => mocks.pinnedStore.ids);
    const idSet = new Set(ids);
    return {
      data: {
        conversations: (mocks.conversations as { id: string }[]).filter((c) => idSet.has(c.id)),
        filterHonored: true,
      },
      isSuccess: true,
    };
  },
  useTogglePinnedConversation: () => ({
    mutate: ({ id, pinned }: { id: string; pinned: boolean }) =>
      mocks.pinnedStore.toggle(id, pinned),
  }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => mocks.rename,
  useArchiveConversation: () => mocks.archiveOverride ?? mocks.archive,
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => mocks.stopSession,
  useProjects: () => ({ data: mocks.projects.map((name: string) => ({ id: `p_${name}`, name })) }),
  // A non-empty `useProjects` renders a project folder, which queries its
  // sessions — return the collapsed (disabled) shape so the folder is inert
  // (this suite keeps its test row unfiled; the picker only needs the name).
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  }),
  useMoveToProject: () => mocks.moveToProject,
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useProjectConfig: () => ({ data: undefined, isLoading: false }),
  useUpdateProjectConfig: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

// Heavy sibling widgets pull their own hooks/providers; stub them so this
// test stays scoped to the conversation row.
vi.mock("./AgentTypeFilter", () => ({ AgentTypeFilter: () => null }));
vi.mock("./ReportIssueButton", () => ({ ReportIssueButton: () => null }));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
// Force a multi-user (non-local) server so the "Shared with me" tab renders —
// jsdom's default loopback origin would otherwise read as single-user and hide
// the tabs the shared-session row actions rely on.
vi.mock("@/lib/serverOrigin", () => ({ isCurrentServerLocal: () => false }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { resetReadStateForTests, seedReadState } from "@/hooks/useUnseenConversations";
import { ROW_DRAG_ACTIVATE_PX, ROW_GESTURE_HOLD_MS } from "@/hooks/useRowGesture";
import { writeSwipeActions } from "@/lib/swipeActionPreferences";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

const CONV: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "My Session",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: {},
  permission_level: null,
  // owner absent → the viewer owns it (rename/share/pin all enabled)
  status: "idle",
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
  // The pinned mock maps its ids onto these loaded conversations.
  mocks.conversations = conversations;
}

/** Full ServerInfo with permissive defaults; override per test. */
function serverInfo(overrides: Partial<ServerInfo> = {}): ServerInfo {
  return {
    accounts_enabled: false,
    single_user: false,
    login_url: null,
    needs_setup: false,
    databricks_features: false,
    managed_sandboxes_enabled: false,
    sandbox_provider: null,
    sharing_mode: "on",
    public_sharing_enabled: true,
    server_version: null,
    smart_routing_enabled: false,
    smart_routing_sources: { external: false, oss: false },
    harness_install_enabled: false,
    installable_harnesses: [],
    dictation_available: false,
    ...overrides,
  };
}

// `activeId` mounts the sidebar at `/c/:conversationId` (via a matching
// Route so `useParams` populates), making that row the active one — the
// rest of the suite renders at `/` where no row is active. `info` pins the
// server sharing policy via CapabilitiesProvider (default "loading" → on).
function renderSidebar(activeId?: string, info?: ServerInfo) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onClose = vi.fn();
  // Build a FRESH element tree per render: re-rendering the identical element
  // reference lets React bail out without re-invoking the sidebar, which
  // would swallow a `mockConversations` swap applied mid-test.
  const makeUi = () => {
    const sidebar = (
      <Suspense fallback={<div data-testid="suspended-sidebar" />}>
        <Sidebar open={true} onClose={onClose} />
      </Suspense>
    );
    const tree = (
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[activeId ? `/c/${activeId}` : "/"]}>
            {activeId ? (
              <Routes>
                <Route path="/c/:conversationId" element={sidebar} />
              </Routes>
            ) : (
              sidebar
            )}
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    );
    // No explicit info → CapabilitiesContext default ("loading"), matching
    // every pre-existing test (sharing treated as on).
    return info ? <CapabilitiesProvider info={info}>{tree}</CapabilitiesProvider> : tree;
  };
  const view = render(makeUi());
  // Re-render so a test can apply a new `mockConversations` list mid-flight
  // (e.g. simulating a reorder pushed between user clicks).
  return Object.assign(view, {
    onClose,
    rerenderSidebar: () => view.rerender(makeUi()),
  });
}

beforeEach(() => {
  mocks.rename.mutate.mockReset();
  mocks.moveToProject.mutate.mockReset();
  // Resolve archive callbacks synchronously so the transient "Archiving…"
  // status row settles back to the interactive row within the test.
  mocks.archive.mutate.mockReset();
  mocks.archive.mutate.mockImplementation(
    (_args: unknown, opts?: { onSuccess?: () => void; onSettled?: () => void }) => {
      opts?.onSuccess?.();
      opts?.onSettled?.();
    },
  );
  // The stop that precedes archiving (runArchive) fires its `onSettled` once
  // the runner stop resolves; invoke it synchronously so the follow-on
  // archive.mutate runs in tests.
  mocks.stopSession.mutate.mockReset();
  mocks.stopSession.mutate.mockImplementation((_id: string, opts?: { onSettled?: () => void }) => {
    opts?.onSettled?.();
  });
  mocks.del.mutate.mockReset();
  mocks.del.reset.mockReset();
  mocks.projects = [];
  mocks.isDragging = null;
  mocks.suspendDraggable = false;
  mocks.archiveOverride = null;
  // Default every test to the desktop viewport; the mobile flyout test opts in.
  mocks.isMobile = false;
  mocks.hasCoarsePointer = false;
  useConvMock.mockReset();
  localStorage.clear();
  // Reset the server pinned set between tests.
  mocks.pinnedStore.set([]);
  // The read-state mirror is module-level (in-memory), so reset it between
  // tests to avoid a mark-unread leaking into later rows.
  resetReadStateForTests();
  mockConversations([CONV]);
});

afterEach(() => {
  cleanup();
  // dnd-kit removes its capture-phase click blocker on a delayed timer.
  if (vi.isFakeTimers()) vi.runOnlyPendingTimers();
  vi.useRealTimers();
});

describe("quick pin/unpin hover button", () => {
  it("keeps the row full-width and the trailing controls inset from the right edge", () => {
    // The row link is `w-full` (not `w-[calc(100%+1rem)]`) so its highlight
    // stays inset from the right edge, aligning with the project/folder rows.
    renderSidebar();

    // The pin + kebab share ONE absolutely-positioned flex container anchored
    // at right-1 with gap-0.5, mirroring the project-folder header actions —
    // so the spacing is defined once and can't drift (no per-button fixed-px
    // offsets like the old right-[30px] / -right-3).
    const pin = screen.getByTestId("quick-pin-conversation");
    const kebab = screen.getByTestId("conversation-actions");
    expect(pin).toHaveClass("size-6");
    expect(kebab).toHaveClass("size-6");
    // Both buttons live in the same wrapper, which owns the position + gap.
    const controls = pin.parentElement!;
    expect(controls).toBe(kebab.parentElement);
    expect(controls).toHaveClass("absolute", "right-1", "flex", "items-center", "gap-0.5");
    // The old per-button offsets are gone.
    expect(pin).not.toHaveClass("right-[30px]", "right-[1.875rem]", "right-[14px]", "absolute");
    expect(kebab).not.toHaveClass("-right-3", "absolute");

    const rowLink = screen.getByRole("link", { name: "My Session" });
    expect(rowLink).toHaveClass("w-full");
    expect(rowLink).not.toHaveClass("w-[calc(100%+1rem)]");
  });

  it("holds action padding while the kebab menu is open", () => {
    renderSidebar();

    const rowLink = screen.getByRole("link", { name: "My Session" });
    expect(rowLink).not.toHaveClass("md:pr-14");

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });

    expect(rowLink).toHaveClass("md:pr-14");
  });

  it("keeps the menu Pin item at desktop widths when a coarse pointer exists", () => {
    // Desktop hides the menu Pin item behind the hover-revealed quick-pin
    // button, but a touch device has no hover at any width — the menu must
    // keep carrying Pin there or wide touch devices cannot pin at all.
    mocks.hasCoarsePointer = true;
    renderSidebar();
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    expect(screen.getByTestId("pin-conversation")).not.toHaveClass("md:hidden");
  });

  it("hides the menu Pin item at desktop widths without a coarse pointer", () => {
    renderSidebar();
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    expect(screen.getByTestId("pin-conversation")).toHaveClass("md:hidden");
  });

  it("sizes the project-folder header controls to match the session-row kebab", () => {
    // The folder-header pencil + kebab share the right-edge column with the
    // session-row kebab, so they must be the same compact `icon-xs` (size-6)
    // button — not the larger `icon-sm` (size-7) — or their glyphs sit in
    // different columns and read as misaligned.
    mocks.projects = ["Sprint 42"];
    renderSidebar();

    expect(screen.getByTestId("project-actions")).toHaveClass("size-6");
    expect(screen.getByTestId("project-actions")).not.toHaveClass("size-7");
    expect(screen.getByTestId("project-new-session")).toHaveClass("size-6");
    expect(screen.getByTestId("project-new-session")).not.toHaveClass("size-7");
    // Same compact size as the session-row kebab it aligns with.
    expect(screen.getByTestId("conversation-actions")).toHaveClass("size-6");
  });

  it("sizes the Projects group-header controls to the same compact icon", () => {
    // The "New project" button and the list-actions kebab (expand-all /
    // select-sessions live inside it) share the right-edge column with the
    // folder + session kebabs, so they use the same compact `icon-xs`
    // (size-6), not the larger `icon-sm` (size-7).
    mocks.projects = ["Sprint 42"];
    renderSidebar();

    expect(screen.getByTestId("new-project")).toHaveClass("size-6");
    expect(screen.getByTestId("new-project")).not.toHaveClass("size-7");
    expect(screen.getByTestId("project-list-actions")).toHaveClass("size-6");
    expect(screen.getByTestId("project-list-actions")).not.toHaveClass("size-7");
  });

  it("hides the Projects list-actions kebab when there are no projects", () => {
    // With no projects, the kebab has nothing to offer (no expand/collapse, no
    // sessions to select) and would open empty — so it's hidden entirely,
    // leaving just the "New project" button.
    mocks.projects = [];
    renderSidebar();

    expect(screen.queryByTestId("project-list-actions")).toBeNull();
    expect(screen.getByTestId("new-project")).toBeInTheDocument();
  });

  it("toggles the pin without opening the kebab menu, moving the row under Pinned", () => {
    renderSidebar();

    // No "Pinned" section to start; the row lives under Recent.
    expect(screen.queryByText("Pinned")).toBeNull();
    const pinButton = screen.getByTestId("quick-pin-conversation");
    expect(pinButton).toHaveAttribute("aria-label", "Pin conversation");

    fireEvent.click(pinButton);

    // The row is now grouped under a "Pinned" header, and the quick button
    // flips to its unpin affordance — both prove the toggle ran through the
    // sidebar's pin state (not just a local no-op).
    const pinnedHeader = screen.getByText("Pinned");
    const pinnedSection = pinnedHeader.closest("section")!;
    expect(within(pinnedSection).getByText("My Session")).toBeInTheDocument();
    expect(screen.getByTestId("quick-pin-conversation")).toHaveAttribute(
      "aria-label",
      "Unpin conversation",
    );

    // Persisted server-side (the `omnigent.pinned` label) so the pin follows the
    // user across devices — same contract as the kebab's Pin item.
    expect(mocks.pinnedStore.ids).toContain("conv_1");

    // Clicking again unpins: the Pinned section disappears.
    fireEvent.click(screen.getByTestId("quick-pin-conversation"));
    expect(screen.queryByText("Pinned")).toBeNull();
  });

  it("also offers Pin in the kebab menu (mobile affordance) and toggles the same pin state", () => {
    renderSidebar();

    expect(screen.queryByText("Pinned")).toBeNull();

    // Radix DropdownMenu opens on pointerdown, not click.
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });

    // The kebab carries a Pin item (mobile-only via `md:hidden`, but always in
    // the DOM since jsdom doesn't evaluate media queries). Clicking it drives
    // the same pin state as the quick button — the row moves under "Pinned".
    const pinItem = screen.getByTestId("pin-conversation");
    expect(pinItem).toHaveTextContent("Pin");
    fireEvent.click(pinItem);

    const pinnedHeader = screen.getByText("Pinned");
    const pinnedSection = pinnedHeader.closest("section")!;
    expect(within(pinnedSection).getByText("My Session")).toBeInTheDocument();
    expect(mocks.pinnedStore.ids).toContain("conv_1");
  });

  it("splits the two pin affordances by viewport via Tailwind responsive classes", () => {
    // jsdom doesn't evaluate CSS media queries, so both affordances live in the
    // DOM regardless of viewport — the mobile/desktop split is purely the
    // responsive classes. Assert those classes directly: the kebab Pin item is
    // hidden from `md` up (desktop), and the quick button is hidden below `md`
    // (mobile) but shown from `md` up. Together they guarantee exactly one pin
    // affordance is visible at any breakpoint.
    renderSidebar();

    // Desktop quick button: hidden on mobile, revealed from `md` up. The reveal
    // uses `md:inline-flex` (not `md:block`) so the button stays a flex
    // container — see the centering regression test below.
    const quickButton = screen.getByTestId("quick-pin-conversation");
    expect(quickButton).toHaveClass("hidden", "md:inline-flex");

    // Kebab Pin item: present in the menu but hidden from `md` up, so it only
    // surfaces on mobile.
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    expect(screen.getByTestId("pin-conversation")).toHaveClass("md:hidden");
  });

  it("reveals the quick-pin button without breaking icon centering", () => {
    // The Button base centers its icon with `inline-flex` + `items-center
    // justify-center`. The desktop reveal MUST keep a flex display: revealing
    // it with `md:block` overrode `inline-flex`, made the
    // centering classes inert, and shoved the pin glyph to the button's
    // top-left corner (~6px off-center). Guard the display so the reveal
    // stays flex and the glyph stays centered.
    renderSidebar();

    const quickButton = screen.getByTestId("quick-pin-conversation");
    // The centering classes are present...
    expect(quickButton).toHaveClass("items-center", "justify-center");
    // ...and the desktop reveal makes the button a flex container (so those
    // classes actually take effect), rather than a block (which would not).
    expect(quickButton).toHaveClass("md:inline-flex");
    expect(quickButton).not.toHaveClass("md:block");
  });
});

describe("double-click to rename", () => {
  it("enters inline rename on double-click and commits the new title on Enter", () => {
    renderSidebar();

    // No edit field until the row is double-clicked.
    expect(screen.queryByTestId("rename-conversation-input")).toBeNull();

    const row = screen.getByRole("link", { name: /My Session/ });
    fireEvent.dblClick(row);

    const input = screen.getByTestId("rename-conversation-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Renamed Session" } });
    fireEvent.keyDown(input, { key: "Enter" });

    // The committed (trimmed) title is forwarded to the rename mutation with
    // the row's id — proving the double-click path drives the same rename as
    // the kebab's Rename item.
    expect(mocks.rename.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.rename.mutate).toHaveBeenCalledWith({ id: "conv_1", title: "Renamed Session" });
  });

  it("does not commit the rename when Enter confirms an active IME composition", () => {
    renderSidebar();

    const row = screen.getByRole("link", { name: /My Session/ });
    fireEvent.dblClick(row);

    const input = screen.getByTestId("rename-conversation-input") as HTMLInputElement;
    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "名前変更" } });

    // The Enter that confirms the conversion candidate must NOT commit.
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename.mutate).not.toHaveBeenCalled();

    // Once composition ends, a subsequent Enter commits as usual.
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.rename.mutate).toHaveBeenCalledWith({ id: "conv_1", title: "名前変更" });
  });

  it("does not commit the rename when Enter carries the IME keyCode 229 fallback", () => {
    renderSidebar();

    const row = screen.getByRole("link", { name: /My Session/ });
    fireEvent.dblClick(row);

    const input = screen.getByTestId("rename-conversation-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(mocks.rename.mutate).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.rename.mutate).toHaveBeenCalledWith({ id: "conv_1", title: "Renamed" });
  });

  it("ignores a double-click whose first click landed on a different row", () => {
    // A native double-click delivers click, click, dblclick to one element.
    // If the list reorders between the two clicks (a session's updated_at
    // bump pushes rows around under the cursor), the second click and the
    // dblclick land on whichever row slid into place — which must NOT enter
    // rename, or the user renames a session they never aimed at.
    const convA: Conversation = {
      ...CONV,
      id: "conv_a",
      title: "Session A",
      updated_at: 1_700_000_200,
    };
    const convB: Conversation = {
      ...CONV,
      id: "conv_b",
      title: "Session B",
      updated_at: 1_700_000_100,
    };
    mockConversations([convA, convB]);
    const view = renderSidebar();

    // Click #1 of the user's double-click lands on session A (the top row).
    fireEvent.click(screen.getByRole("link", { name: /Session A/ }));

    // Before click #2, session B's updated_at bumps and the list reorders —
    // B now occupies the screen position where A was.
    mockConversations([{ ...convB, updated_at: 1_700_000_300 }, convA]);
    view.rerenderSidebar();

    // Click #2 and the dblclick land on B, the row now under the cursor.
    const rowB = screen.getByRole("link", { name: /Session B/ });
    fireEvent.click(rowB);
    fireEvent.dblClick(rowB);

    // B saw only the second click, so it must not enter rename.
    expect(screen.queryByTestId("rename-conversation-input")).toBeNull();
    expect(mocks.rename.mutate).not.toHaveBeenCalled();

    // A clean double-click on B — both clicks on the row — still renames it.
    fireEvent.click(rowB);
    fireEvent.click(rowB);
    fireEvent.dblClick(rowB);
    const input = screen.getByTestId("rename-conversation-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Renamed B" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename.mutate).toHaveBeenCalledWith({ id: "conv_b", title: "Renamed B" });
  });

  it("does not enter rename on double-click for a viewer-only row", () => {
    // Rename is owner-only now, so a session owned by another user has its
    // kebab Rename item disabled and double-click must be inert too. A
    // non-owner session lives on the "Shared with me" tab, so switch to it
    // before reaching for the row.
    mockConversations([{ ...CONV, owner: "other@example.com" }]);
    renderSidebar();
    // Radix Tabs triggers activate on mousedown (primary button), not click.
    fireEvent.pointerDown(screen.getByTestId("session-filter"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    fireEvent.click(screen.getByTestId("session-filter-shared"));

    fireEvent.dblClick(screen.getByRole("link", { name: /My Session/ }));

    expect(screen.queryByTestId("rename-conversation-input")).toBeNull();
    expect(mocks.rename.mutate).not.toHaveBeenCalled();
  });
});

describe("pinned row project flyout", () => {
  // Pinning lifts a session out of its project folder into the flat "Pinned"
  // section, so the folder no longer conveys which project it came from. The
  // hover flyout restores that cue: title + folder icon + project name. It
  // opens on focus/hover — fire focus on the row link and await the portal.

  it("shows the project name in the flyout for a pinned, project-owned row", async () => {
    // Seed the pin so the row lifts into the always-expanded Pinned section
    // (a project-owned row otherwise sits inside a collapsed project folder).
    mocks.pinnedStore.set(["conv_1"]);
    mockConversations([
      {
        ...CONV,
        labels: { omni_project: "Moonshot" },
        git_branch: "fix/sidebar-row-height",
      },
    ]);
    renderSidebar();
    expect(screen.getByText("Pinned")).toBeInTheDocument();

    // Focus opens the HoverCard (onFocus is one of its open triggers); the
    // content is portalled, so query the whole document after the open delay.
    fireEvent.focus(screen.getByRole("link", { name: /My Session/ }));
    const flyout = await screen.findByTestId("pinned-project-flyout");
    expect(within(flyout).getByText("Moonshot")).toBeInTheDocument();
    const flyoutTitle = within(flyout).getByText("My Session");
    expect(flyoutTitle).toBeInTheDocument();
    // The flyout title is sized to match the sidebar row name
    // (`sidebar-compact-text`, 13px at the default), not the larger `text-sm`.
    // Both scale with the UI font-size setting via the rem-based root.
    expect(flyoutTitle).toHaveClass("sidebar-compact-text");
    expect(flyoutTitle).not.toHaveClass("text-sm");
    expect(within(flyout).getByTestId("pinned-project-flyout-branch")).toHaveTextContent(
      "fix/sidebar-row-height",
    );
  });

  it("renders no project flyout for a pinned row with no project", () => {
    // No project label → nothing to surface, so the row keeps its plain native
    // title tooltip and never mounts a hover-card trigger.
    mocks.pinnedStore.set(["conv_1"]);
    mockConversations([{ ...CONV, labels: {} }]);
    renderSidebar();
    expect(screen.getByText("Pinned")).toBeInTheDocument();

    const row = screen.getByRole("link", { name: /My Session/ });
    expect(row).not.toHaveAttribute("data-slot", "hover-card-trigger");
    fireEvent.focus(row);
    expect(screen.queryByTestId("pinned-project-flyout")).toBeNull();
  });

  it("disables the flyout on a mobile viewport, keeping the native title", () => {
    // Mobile has no real hover, so the flyout is gated off there: a tap that
    // navigates must not also open (and strand) a HoverCard over the chat. The
    // row falls back to the plain link path — no hover-card trigger, native
    // title restored — even though it IS pinned + project-owned.
    mocks.isMobile = true;
    mocks.pinnedStore.set(["conv_1"]);
    mockConversations([{ ...CONV, labels: { omni_project: "Moonshot" } }]);
    renderSidebar();
    expect(screen.getByText("Pinned")).toBeInTheDocument();

    const row = screen.getByRole("link", { name: /My Session/ });
    // No hover-card trigger is mounted, and the native title tooltip is kept.
    expect(row).not.toHaveAttribute("data-slot", "hover-card-trigger");
    expect(row).toHaveAttribute("title", "My Session");
    // Focusing the row opens nothing — the flyout never mounts on mobile.
    fireEvent.focus(row);
    expect(screen.queryByTestId("pinned-project-flyout")).toBeNull();
  });
});

describe("mobile in-place project picker", () => {
  // Desktop opens the "Add to project" item as a side-flyout submenu, but a
  // side flyout has no room on mobile. There the item instead swaps the kebab
  // body in place: the main actions are replaced by the project picker (search
  // + list) plus a Back control that returns to the main menu — no submenu, no
  // close/reopen. Desktop keeps the flyout untouched.

  it("swaps the kebab body to the project picker in place, and Back returns", () => {
    mocks.isMobile = true;
    mocks.projects = ["Sprint 42"];
    renderSidebar();

    // Radix DropdownMenu opens on pointerdown, not click.
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });

    // Main view shows the everyday actions and the (unfiled) project entry.
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("delete-conversation")).toBeInTheDocument();
    const moveItem = screen.getByTestId("move-to-project");
    expect(moveItem).toHaveTextContent("Add to project");
    // It's a plain item on mobile, NOT a side-flyout submenu trigger.
    expect(moveItem).not.toHaveAttribute("aria-haspopup", "menu");

    // Tapping it swaps the body in place to the project picker — the main
    // actions are gone, and the picker (search + project list) plus a Back
    // control are shown.
    fireEvent.click(moveItem);
    expect(screen.getByPlaceholderText("Search projects")).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Sprint 42/ })).toBeInTheDocument();
    expect(screen.getByTestId("project-picker-back")).toBeInTheDocument();
    // The main actions are no longer rendered — the body was replaced, not
    // stacked beside a flyout.
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
    expect(screen.queryByTestId("delete-conversation")).toBeNull();

    // Back returns to the main menu without closing it: the everyday actions
    // are visible again and the picker is gone.
    fireEvent.click(screen.getByTestId("project-picker-back"));
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("delete-conversation")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Search projects")).toBeNull();
  });

  it("moves the session into a picked project just like desktop", () => {
    mocks.isMobile = true;
    mocks.projects = ["Sprint 42"];
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("move-to-project"));
    fireEvent.click(screen.getByRole("menuitem", { name: /Sprint 42/ }));

    // Same mutation contract as the desktop submenu pick.
    expect(mocks.moveToProject.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      project: "Sprint 42",
    });
  });

  it("keeps the desktop side-flyout submenu (no in-place swap)", () => {
    // Desktop viewport (default). The project entry is a submenu trigger, and
    // opening the kebab never renders the in-place Back control.
    mocks.projects = ["Sprint 42"];
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    const moveItem = screen.getByTestId("move-to-project");
    // Radix SubTrigger advertises a submenu popup.
    expect(moveItem).toHaveAttribute("aria-haspopup", "menu");
    expect(screen.queryByTestId("project-picker-back")).toBeNull();
  });
});

describe("mark as unread", () => {
  it("re-lights the row's unread dot via an explicit mark-unread", () => {
    renderSidebar();

    // The row starts seen (no baseline) — no unread marker.
    expect(screen.queryByText("(unread)")).toBeNull();

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("mark-unread-conversation"));

    // The dot's accessible label appears immediately (in-tab tick on the
    // optimistic mirror write); the baseline is also synced to the server.
    expect(screen.getByText("(unread)")).toBeInTheDocument();
  });

  it("holds the dot on a running session until the turn finishes", () => {
    mockConversations([{ ...CONV, status: "running" }]);
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("mark-unread-conversation"));

    // The dot stays suppressed mid-turn (the explicit override lifts the
    // active-row suppression, not the running one).
    expect(screen.queryByText("(unread)")).toBeNull();

    // Once the turn finishes (row re-renders as idle), the dot lights — the
    // baseline (kept in the in-memory mirror) now reads unseen for a
    // finished session.
    cleanup();
    mockConversations([{ ...CONV, status: "idle" }]);
    renderSidebar();
    expect(screen.getByText("(unread)")).toBeInTheDocument();
  });

  it("lights the dot on the active thread you're currently viewing", () => {
    // The active row normally suppresses the dot (you're reading it), but an
    // explicit mark-unread is a deliberate flag, so the dot must show.
    renderSidebar("conv_1");

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("mark-unread-conversation"));

    expect(screen.getByText("(unread)")).toBeInTheDocument();
  });

  it("is hidden once the row is already unread", () => {
    // Seed a baseline below updated_at (as the conversation list would) so
    // the row is already unseen.
    seedReadState([{ id: "conv_1", viewer_last_seen: CONV.updated_at - 1 }]);
    renderSidebar();

    expect(screen.getByText("(unread)")).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    expect(screen.queryByTestId("mark-unread-conversation")).toBeNull();
  });
});

describe("right-click context menu", () => {
  it("does not open while the row is being dragged", () => {
    mocks.isDragging = true;
    renderSidebar();

    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));

    expect(screen.queryByTestId("rename-conversation")).toBeNull();
  });

  it("leaves keyboard link activation untouched after a drag ends", async () => {
    mocks.isMobile = true;
    mocks.isDragging = true;
    const view = renderSidebar();

    mocks.isDragging = false;
    view.rerenderSidebar();
    await act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, 0);
        }),
    );

    // Enter/Space activation reaches a link as a click with no pointerdown.
    // The drag guard must not require or consume a preceding pointer gesture.
    fireEvent.click(screen.getByRole("link", { name: /My Session/ }), { detail: 0 });
    expect(view.onClose).toHaveBeenCalledOnce();
  });

  it("opens the same action items as the kebab and drives the same handlers", () => {
    renderSidebar();

    // Nothing in the DOM until the row is right-clicked (the kebab menu is
    // closed, so its items aren't rendered either).
    expect(screen.queryByTestId("rename-conversation")).toBeNull();

    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));

    // The context menu carries the full set of kebab actions — same testids,
    // so it renders from the shared ConversationMenuItems body.
    expect(screen.getByTestId("share-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("move-to-project")).toBeInTheDocument();
    expect(screen.getByTestId("archive-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("delete-conversation")).toBeInTheDocument();

    // Selecting Rename runs the same path as the kebab / double-click: the
    // inline rename input appears.
    fireEvent.click(screen.getByTestId("rename-conversation"));
    expect(screen.getByTestId("rename-conversation-input")).toBeInTheDocument();
  });

  it("holds the list order under the pointer so a right-click rename hits the aimed row", () => {
    // A right-click is a single event, so nothing can cross-check it like the
    // double-click guard does — if the list reorders in the instant before
    // the click lands, the row that slid under the cursor opens its (visually
    // identical) menu and gets renamed. The fix is upstream: while the
    // pointer is inside the list, every row's sort key is frozen so rows
    // can't move under the cursor at all.
    const convA: Conversation = {
      ...CONV,
      id: "conv_a",
      title: "Session A",
      updated_at: 1_700_000_200,
    };
    const convB: Conversation = {
      ...CONV,
      id: "conv_b",
      title: "Session B",
      updated_at: 1_700_000_100,
    };
    mockConversations([convA, convB]);
    const view = renderSidebar();

    // The pointer moves over the list, aiming at session A (the top row).
    fireEvent.mouseOver(screen.getByTestId("sidebar-conversation-list"));

    // Session B's updated_at bumps past A before the right-click lands.
    mockConversations([{ ...convB, updated_at: 1_700_000_300 }, convA]);
    view.rerenderSidebar();

    // The order holds: A is still the top row, exactly where the user aims.
    const links = screen.getAllByRole("link", { name: /^Session/ });
    expect(links[0]).toHaveAccessibleName(/Session A/);

    // Right-click the top row and rename it — the PATCH targets session A.
    fireEvent.contextMenu(links[0]);
    fireEvent.click(screen.getByTestId("rename-conversation"));
    const input = screen.getByTestId("rename-conversation-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Renamed A" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename.mutate).toHaveBeenCalledWith({ id: "conv_a", title: "Renamed A" });
  });

  it("keeps the order held while a rename edit is open even after the pointer leaves", () => {
    // The pointer naturally drifts out of the sidebar while typing a new
    // title. If that released the freeze, background updated_at churn would
    // shuffle rows around the open input — and moving the input's DOM node
    // blurs it, committing a half-typed title. An open rename edit must hold
    // the order on its own; the snap-back happens once the edit ends.
    const convA: Conversation = {
      ...CONV,
      id: "conv_a",
      title: "Session A",
      updated_at: 1_700_000_200,
    };
    const convB: Conversation = {
      ...CONV,
      id: "conv_b",
      title: "Session B",
      updated_at: 1_700_000_100,
    };
    mockConversations([convA, convB]);
    const view = renderSidebar();

    // Open the rename input on session A via right-click → Rename.
    fireEvent.mouseOver(screen.getByTestId("sidebar-conversation-list"));
    fireEvent.contextMenu(screen.getByRole("link", { name: /Session A/ }));
    fireEvent.click(screen.getByTestId("rename-conversation"));
    const input = screen.getByTestId("rename-conversation-input") as HTMLInputElement;

    // The pointer leaves the list mid-edit; then session B's updated_at bumps.
    fireEvent.mouseOut(screen.getByTestId("sidebar-conversation-list"), {
      relatedTarget: document.body,
    });
    mockConversations([{ ...convB, updated_at: 1_700_000_300 }, convA]);
    view.rerenderSidebar();

    // The edit row still holds the top slot (the edit input replaces A's link,
    // so B — below it — must still be the only link, not the first row).
    expect(screen.getByTestId("rename-conversation-input")).toBe(input);
    const listEl = screen.getByTestId("sidebar-conversation-list");
    const rowB = screen.getByRole("link", { name: /Session B/ });
    expect(input.compareDocumentPosition(rowB) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(listEl).toContainElement(rowB);

    // Committing the rename targets session A and releases the hold: the
    // order snaps to reality (B first).
    fireEvent.change(input, { target: { value: "Renamed A" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename.mutate).toHaveBeenCalledWith({ id: "conv_a", title: "Renamed A" });
    const after = screen.getAllByRole("link", { name: /^Session/ });
    expect(after[0]).toHaveAccessibleName(/Session B/);
  });

  it("snaps the order back to reality when the pointer leaves the list", () => {
    const convA: Conversation = {
      ...CONV,
      id: "conv_a",
      title: "Session A",
      updated_at: 1_700_000_200,
    };
    const convB: Conversation = {
      ...CONV,
      id: "conv_b",
      title: "Session B",
      updated_at: 1_700_000_100,
    };
    mockConversations([convA, convB]);
    const view = renderSidebar();

    fireEvent.mouseOver(screen.getByTestId("sidebar-conversation-list"));
    mockConversations([{ ...convB, updated_at: 1_700_000_300 }, convA]);
    view.rerenderSidebar();
    expect(screen.getAllByRole("link", { name: /^Session/ })[0]).toHaveAccessibleName(/Session A/);

    fireEvent.mouseOut(screen.getByTestId("sidebar-conversation-list"), {
      relatedTarget: document.body,
    });
    expect(screen.getAllByRole("link", { name: /^Session/ })[0]).toHaveAccessibleName(/Session B/);
  });
});

describe("row context menus render non-modally", () => {
  // react-remove-scroll's modal lock hit-tests through the sidebar overlay to
  // the chat behind it (pointer-events: none on body only blocks the sidebar's
  // own subtree from taking the hit, not the underlying page). modal={false}
  // must be set on all three row variants so a touch continuing past the menu
  // never reaches native text selection in the chat.
  it("does not lock body pointer-events for the desktop context menu", () => {
    renderSidebar();
    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(document.body).not.toHaveAttribute("data-scroll-locked");
    expect(document.body.style.pointerEvents).not.toBe("none");
  });

  it("does not lock body pointer-events for the mobile context menu", () => {
    mocks.isMobile = true;
    renderSidebar();
    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(document.body).not.toHaveAttribute("data-scroll-locked");
    expect(document.body.style.pointerEvents).not.toBe("none");
  });

  it("does not lock body pointer-events for a pinned row's project-flyout context menu", () => {
    mocks.pinnedStore.set(["conv_1"]);
    mockConversations([{ ...CONV, labels: { omni_project: "Moonshot" } }]);
    renderSidebar();
    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(document.body).not.toHaveAttribute("data-scroll-locked");
    expect(document.body.style.pointerEvents).not.toBe("none");
  });

  it("marks the menu content select-none with a scrollable, page-safe touch-action", () => {
    renderSidebar();
    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));
    const menu = screen.getByRole("menu");
    expect(menu).toHaveClass("select-none");
    expect(menu).toHaveClass("touch-pan-y");
    expect(menu).not.toHaveClass("touch-none");
  });
});

const TOUCH_POINTER = { pointerId: 1, isPrimary: true, pointerType: "touch" as const };
// A second finger: non-primary, so the recognizer cancels the gesture in flight.
const SECOND_TOUCH_POINTER = { pointerId: 2, isPrimary: false, pointerType: "touch" as const };

function touchTarget() {
  // ContextMenu marks the app tree aria-hidden while portalled; pointer capture
  // still routes the held touch to this same row underneath it.
  const link = screen.getByRole("link", { name: /My Session/, hidden: true });
  return { link, row: link.closest("li")! };
}

function touchPoint(target: Element, x: number, y: number, identifier = 0) {
  return {
    identifier,
    target,
    clientX: x,
    clientY: y,
    pageX: x,
    pageY: y,
    screenX: x,
    screenY: y,
  };
}

function startTouch(x = 100, y = 100) {
  const { link } = touchTarget();
  const touch = touchPoint(link, x, y);
  fireEvent.pointerDown(link, { ...TOUCH_POINTER, clientX: x, clientY: y });
  fireEvent.touchStart(link, { touches: [touch], changedTouches: [touch] });
}

function moveTouch(x: number, y: number, touches = 1) {
  const { link } = touchTarget();
  const points = Array.from({ length: touches }, (_, index) =>
    touchPoint(link, x + index, y + index, index),
  );
  const allowsNativeScroll = fireEvent.pointerMove(link, {
    ...TOUCH_POINTER,
    clientX: x,
    clientY: y,
  });
  fireEvent.touchMove(link, { touches: points, changedTouches: points });
  return allowsNativeScroll;
}

function endTouch(x = 100, y = 100) {
  const { link } = touchTarget();
  const touch = touchPoint(link, x, y);
  fireEvent.pointerUp(link, { ...TOUCH_POINTER, clientX: x, clientY: y });
  fireEvent.touchEnd(link, { touches: [], changedTouches: [touch] });
}

describe("touch gesture arbitration", () => {
  beforeEach(() => {
    mocks.hasCoarsePointer = true;
  });

  function advanceHold(ms = ROW_GESTURE_HOLD_MS) {
    act(() => vi.advanceTimersByTime(ms));
  }

  it("opens once at hold fire without Radix adding a second menu", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { link, row } = touchTarget();
    const contextMenus: MouseEvent[] = [];
    link.addEventListener("contextmenu", (event) => contextMenus.push(event));

    // The recognizer opens first; holding past Radix's 700ms timer must not
    // add a second menu or dispatch another contextmenu event.
    startTouch(140, 220);
    advanceHold();
    expect(document.querySelectorAll('[role="menu"]')).toHaveLength(1);
    expect(contextMenus).toHaveLength(1);
    expect(row).toHaveClass("scale-[1.01]");

    advanceHold(500);
    expect(document.querySelectorAll('[role="menu"]')).toHaveLength(1);
    expect(contextMenus).toHaveLength(1);
    endTouch(140, 220);
    expect(document.querySelectorAll('[role="menu"]')).toHaveLength(1);
    expect(contextMenus).toHaveLength(1);
  });

  it("leaves a pen long-press to Radix, which the recognizer never claims", () => {
    // The recognizer is touch-only, so suppressing Radix for pen would strip
    // stylus users of the long-press menu they had before.
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { link } = touchTarget();
    const pen = { pointerId: 1, isPrimary: true, pointerType: "pen" as const };

    fireEvent.pointerDown(link, { ...pen, clientX: 140, clientY: 220 });
    act(() => vi.advanceTimersByTime(900));

    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
  });

  it("opens at the current finger point and stays open through release and trailing click", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    const view = renderSidebar();
    const { link, row } = touchTarget();
    const contextMenus: MouseEvent[] = [];
    link.addEventListener("contextmenu", (event) => contextMenus.push(event));

    startTouch(140, 220);
    moveTouch(145, 224);
    advanceHold();
    expect(row).toHaveClass("scale-[1.01]");
    expect(row).not.toHaveClass("opacity-40");
    expect(screen.getByTestId("archive-conversation")).toBeInTheDocument();
    expect(contextMenus.at(-1)).toMatchObject({ clientX: 145, clientY: 224 });

    endTouch(145, 224);
    fireEvent.click(link);
    expect(screen.getByTestId("archive-conversation")).toBeInTheDocument();
    expect(row).not.toHaveClass("opacity-40");
    expect(contextMenus).toHaveLength(1);
    expect(view.onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("archive-conversation"));
    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
  });

  it("suppresses the OS's native contextmenu while the recognizer owns the touch", () => {
    // Android fires its own contextmenu at ~500ms — after the 400ms arm
    // already opened the menu. Capture retargets it to the row root,
    // bypassing Radix's trigger; unprevented it starts text selection and
    // cancels the pointer stream, killing the pending drag. The recognizer's
    // own dispatch carries a tag; the OS's does not.
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    advanceHold();
    // The tagged synthetic dispatch got through: the menu is open.
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();

    const nativeLongPress = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    row.dispatchEvent(nativeLongPress);
    expect(nativeLongPress.defaultPrevented).toBe(true);

    // The OS hit-tests the element under the finger — once the menu opens
    // there, that's the menu portal, far from the row. The document-level
    // guard must still catch it.
    const offRowLongPress = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    document.body.dispatchEvent(offRowLongPress);
    expect(offRowLongPress.defaultPrevented).toBe(true);

    const selection = new Event("selectstart", { bubbles: true, cancelable: true });
    document.body.dispatchEvent(selection);
    expect(selection.defaultPrevented).toBe(true);
    endTouch();

    // Released: the guards are gone.
    const afterRelease = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    document.body.dispatchEvent(afterRelease);
    expect(afterRelease.defaultPrevented).toBe(false);
  });

  it("renders the row link non-draggable so a stationary long-press survives", () => {
    // Links are natively draggable; Chrome Android hands a stationary
    // long-press on one to native drag with an unconditional pointercancel —
    // fired before the contextmenu event, so no preventDefault can save the
    // pointer stream. draggable=false removes that claimant entirely.
    renderSidebar();
    const rowLink = screen.getByRole("link", { name: /My Session/ });
    expect(rowLink).toHaveAttribute("draggable", "false");
  });

  it("leaves the contextmenu alone when no gesture owns the touch", () => {
    // Desktop right-click arrives with the recognizer idle — it must reach
    // Radix untouched or the mouse context menu dies.
    renderSidebar();
    const { row } = touchTarget();

    const rightClick = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    row.dispatchEvent(rightClick);
    expect(rightClick.defaultPrevented).toBe(false);
  });

  it("keeps a threshold-minus-one wiggle armed with the menu open", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    advanceHold();
    expect(row).not.toHaveClass("opacity-40");
    expect(row).toHaveClass("touch-none");
    expect(row).not.toHaveClass("touch-pan-y");
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();

    moveTouch(104, 104);
    moveTouch(100 + ROW_DRAG_ACTIVATE_PX - 1, 100);
    expect(row).not.toHaveClass("opacity-40");
    expect(row).toHaveClass("touch-none");
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    endTouch(100 + ROW_DRAG_ACTIVATE_PX - 1, 100);
  });

  it("measures the drag threshold from the arm point, not the press origin", () => {
    // A press may legally drift up to the hold tolerance before the timer
    // fires; that drift must not pre-spend the drag budget, or the first
    // post-arm tremble would dismiss the fresh menu into a drag.
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    // Vertical-dominant 15px drift: inside the 20px hold tolerance, outside
    // the swipe wedge, under the 25px scroll circle.
    moveTouch(100, 115);
    advanceHold();
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();

    // ~3px wiggle from the ARM point (but ~17px from the press origin).
    moveTouch(102, 117);
    expect(row).not.toHaveClass("opacity-40");
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();

    // A deliberate pull measured from the arm point still drags.
    moveTouch(100, 115 + ROW_DRAG_ACTIVATE_PX);
    expect(row).toHaveClass("opacity-40");
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
    endTouch(100, 115 + ROW_DRAG_ACTIVATE_PX);
  });

  it("starts drag at the threshold without a menu-dismissal keydown", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();
    const escapeKeydowns = vi.fn((event: KeyboardEvent) => event.key);
    document.addEventListener("keydown", escapeKeydowns, { capture: true });

    startTouch();
    advanceHold();
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();

    moveTouch(100 + ROW_DRAG_ACTIVATE_PX, 100);
    expect(row).toHaveClass("opacity-40");
    expect(row).toHaveClass("touch-none");
    expect(screen.queryByTestId("rename-conversation")).toBeNull();

    moveTouch(100 + ROW_DRAG_ACTIVATE_PX + 10, 100);
    expect(row).toHaveClass("opacity-40");
    document.removeEventListener("keydown", escapeKeydowns, { capture: true });
    expect(escapeKeydowns).not.toHaveBeenCalled();
    endTouch(100 + ROW_DRAG_ACTIVATE_PX + 10, 100);
  });

  it("does not restart a drag after viewport resize cancels the sensor", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    advanceHold();
    moveTouch(100 + ROW_DRAG_ACTIVATE_PX, 100);
    expect(row).toHaveClass("opacity-40");

    act(() => window.dispatchEvent(new Event("resize")));
    expect(row).not.toHaveClass("opacity-40");

    moveTouch(100 + ROW_DRAG_ACTIVATE_PX + 1, 100);
    expect(row).not.toHaveClass("opacity-40");
    endTouch(100 + ROW_DRAG_ACTIVATE_PX + 1, 100);
  });

  it("resets the recognizer when dnd-kit ends the drag", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { link, row } = touchTarget();

    startTouch();
    advanceHold();
    moveTouch(100 + ROW_DRAG_ACTIVATE_PX, 100);
    expect(row).toHaveClass("opacity-40");

    const touch = touchPoint(link, 100 + ROW_DRAG_ACTIVATE_PX, 100);
    fireEvent.touchEnd(link, { touches: [], changedTouches: [touch] });

    expect(row).not.toHaveClass("opacity-40");
    expect(row).not.toHaveClass("touch-none");
    moveTouch(100 + ROW_DRAG_ACTIVATE_PX + 1, 100);
    expect(row).not.toHaveClass("opacity-40");
  });

  it("arms after ordinary sixteen-pixel hold drift", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    moveTouch(100, 116);
    advanceHold();

    expect(row).toHaveClass("scale-[1.01]");
    endTouch(100, 116);
  });

  it("arms at exactly 20px of hold drift", () => {
    vi.useFakeTimers();
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    moveTouch(112, 116);
    advanceHold();

    expect(row).toHaveClass("scale-[1.01]");
    endTouch(112, 116);
  });

  it("rejects 21px of hold drift below the scroll boundary", () => {
    // A slow scroll drifts under the 25px scroll threshold but is not holding
    // still. Arming here would capture the pointer and hand the gesture to
    // dnd-kit, which preventDefaults the scroll away for the rest of the touch.
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    // Reach 21px before the timer: past hold tolerance, still short of scroll.
    for (let step = 1; step <= 3; step += 1) {
      advanceHold(90);
      moveTouch(100, 100 + step * 7);
    }
    advanceHold(130);

    expect(row).not.toHaveClass("scale-[1.01]");
    expect(row).not.toHaveClass("opacity-40");
    endTouch(100, 121);
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
  });

  it("leaves a normal-speed vertical flick to the list scroller", () => {
    // Scrolling must stay native even though the finger starts on a row: the
    // flick clears 25px within a frame, long before the hold could arm.
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    advanceHold(16);
    expect(moveTouch(100, 125)).toBe(true);
    advanceHold(600);

    expect(row).not.toHaveClass("scale-[1.01]");
    expect(row).not.toHaveClass("opacity-40");
    endTouch(100, 125);
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
  });

  it("keeps a slow pre-hold horizontal gesture as a swipe", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    advanceHold(260);
    moveTouch(94, 100);
    advanceHold(80);
    moveTouch(80, 100);
    expect(row).not.toHaveClass("opacity-40");
    moveTouch(10, 100);
    expect(row.querySelector<HTMLElement>("div.relative")?.style.marginRight).not.toBe("");
    endTouch(10, 100);

    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
  });

  it("yields vertical travel to native scroll with no other outcome", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    expect(moveTouch(100, 125)).toBe(true);
    advanceHold(ROW_GESTURE_HOLD_MS + 1);
    endTouch(100, 125);

    expect(row).not.toHaveClass("opacity-40");
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("assigns vertical-dominant travel exactly at the 25px boundary to scroll", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    // The 15-20-25 triangle pins the Euclidean boundary with integer deltas.
    startTouch();
    expect(moveTouch(115, 120)).toBe(true);

    // Decided here, at move time — not later by the hold's drift check. Turning
    // back onto the horizontal axis must stay inert, which it only does if the
    // gesture already resolved to scroll.
    moveTouch(60, 120);
    expect(row.querySelector<HTMLElement>("div.relative")?.style.marginRight).toBe("");

    advanceHold(ROW_GESTURE_HOLD_MS + 1);
    endTouch(60, 120);

    expect(row).not.toHaveClass("opacity-40");
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("stops tracking when a locked swipe reverses into an inert direction", () => {
    vi.useFakeTimers();
    // Left archives; right is inert. Locking left then dragging back past the
    // origin crosses into "none", which must not translate the row with an
    // empty reveal behind it.
    writeSwipeActions({ left: "archive", right: "none" });
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();
    const surface = row.querySelector<HTMLElement>("div.relative");

    startTouch();
    moveTouch(80, 100);
    expect(surface?.style.marginRight).toBe("20px");

    moveTouch(190, 100);
    expect(surface?.style.marginLeft).toBe("");
    expect(surface?.style.marginRight).toBe("");

    endTouch(190, 100);
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("keeps 24px below the explicit scroll boundary pending", () => {
    vi.useFakeTimers();
    writeSwipeActions({ left: "archive", right: "archive" });
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();
    const surface = row.querySelector<HTMLElement>("div.relative");

    // The browser normally claims this through pan-y. Without pointercancel,
    // the explicit fallback must remain pending until the exact 25px boundary.
    startTouch();
    moveTouch(100, 124);
    expect(surface?.style.marginRight).toBe("");
    moveTouch(10, 100);
    expect(surface?.style.marginRight).not.toBe("");
    endTouch(10, 100);

    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
  });

  it("keeps 11px horizontal travel below swipe activation", () => {
    vi.useFakeTimers();
    writeSwipeActions({ left: "archive", right: "archive" });
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    moveTouch(111, 100);
    const surface = row.querySelector<HTMLElement>("div.relative");
    expect(surface?.style.marginLeft).toBe("");
    advanceHold();
    expect(row).toHaveClass("scale-[1.01]");
    endTouch(111, 100);
  });

  it("assigns exactly 12px horizontal travel to swipe", () => {
    vi.useFakeTimers();
    writeSwipeActions({ left: "archive", right: "archive" });
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    moveTouch(112, 100);
    const surface = row.querySelector<HTMLElement>("div.relative");
    expect(surface?.style.marginLeft).toBe("12px");
    advanceHold(ROW_GESTURE_HOLD_MS + 1);
    expect(row).not.toHaveClass("opacity-40");
    endTouch(112, 100);

    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
  });

  it("arms touch drag on a wide viewport with a coarse pointer", () => {
    vi.useFakeTimers();
    mocks.isMobile = false;
    renderSidebar();
    const { row } = touchTarget();

    startTouch();
    advanceHold();
    expect(row).toHaveClass("scale-[1.01]");
    moveTouch(100 + ROW_DRAG_ACTIVATE_PX, 100);

    expect(row).toHaveClass("opacity-40");
    endTouch(100 + ROW_DRAG_ACTIVATE_PX, 100);
  });

  it("leaves a short still press as plain link activation", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    const view = renderSidebar();
    const { link, row } = touchTarget();

    startTouch();
    advanceHold(ROW_GESTURE_HOLD_MS - 1);
    endTouch();
    fireEvent.click(link);

    expect(view.onClose).toHaveBeenCalledOnce();
    expect(row).not.toHaveClass("opacity-40");
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("does not swallow a mouse click after a desktop touch scroll gesture", () => {
    vi.useFakeTimers();
    const view = renderSidebar();
    const { link } = touchTarget();

    startTouch();
    moveTouch(80, 100);
    endTouch(80, 100);
    fireEvent.pointerDown(link, {
      pointerType: "mouse",
      button: 0,
      isPrimary: true,
      pointerId: 9,
    });
    fireEvent.click(link);

    expect(view.onClose).toHaveBeenCalledOnce();
  });

  it("selects a row on the first tap after a short swipe", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    startTouch();
    moveTouch(80, 100);
    endTouch(80, 100);
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
    const { link } = touchTarget();
    fireEvent.pointerDown(link, { ...TOUCH_POINTER, clientX: 100, clientY: 100 });
    fireEvent.click(link);

    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("fully resets when the pointer is cancelled", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { link, row } = touchTarget();

    startTouch();
    moveTouch(80, 100);
    const surface = row.querySelector<HTMLElement>("div.relative");
    expect(surface?.style.marginRight).toBe("20px");
    fireEvent.pointerCancel(link, { ...TOUCH_POINTER, clientX: 80, clientY: 100 });
    fireEvent.touchCancel(link, { touches: [], changedTouches: [touchPoint(link, 80, 100)] });
    advanceHold(ROW_GESTURE_HOLD_MS + 1);

    expect(surface?.style.marginRight).toBe("");
    expect(row).not.toHaveClass("opacity-40");
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("rejects a second touch before drag activation", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { link, row } = touchTarget();

    startTouch();
    fireEvent.pointerDown(link, {
      pointerId: 2,
      isPrimary: false,
      pointerType: "touch",
      clientX: 104,
      clientY: 104,
    });
    const touches = [touchPoint(link, 100, 100), touchPoint(link, 104, 104, 1)];
    fireEvent.touchStart(link, { touches, changedTouches: [touches[1]] });
    advanceHold(ROW_GESTURE_HOLD_MS + 1);
    moveTouch(120, 100, 2);

    expect(row).not.toHaveClass("opacity-40");
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
    fireEvent.pointerCancel(link, { ...TOUCH_POINTER, clientX: 120, clientY: 100 });
  });

  it("cancels the first row when a second touch starts on another row", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    mockConversations([
      { ...CONV, id: "conv_a", title: "Session A" },
      { ...CONV, id: "conv_b", title: "Session B" },
    ]);
    renderSidebar();
    const linkA = screen.getByRole("link", { name: /Session A/ });
    const linkB = screen.getByRole("link", { name: /Session B/ });
    const first = touchPoint(linkA, 100, 100);
    const second = touchPoint(linkB, 104, 104, 1);

    fireEvent.pointerDown(linkA, { ...TOUCH_POINTER, clientX: 100, clientY: 100 });
    fireEvent.touchStart(linkA, { touches: [first], changedTouches: [first] });
    fireEvent.pointerDown(linkB, { ...SECOND_TOUCH_POINTER, clientX: 104, clientY: 104 });
    fireEvent.touchStart(linkB, {
      touches: [first, second],
      changedTouches: [second],
    });

    advanceHold(ROW_GESTURE_HOLD_MS + 1);
    expect(linkA.closest("li")).not.toHaveClass("scale-[1.01]");
    fireEvent.pointerMove(linkA, { ...TOUCH_POINTER, clientX: 10, clientY: 100 });
    fireEvent.pointerUp(linkA, { ...TOUCH_POINTER, clientX: 10, clientY: 100 });

    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(linkA.closest("li")).not.toHaveClass("scale-[1.01]");
  });

  it("cancels dnd-kit when a second touch starts on another row mid-drag", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    mockConversations([
      { ...CONV, id: "conv_a", title: "Session A" },
      { ...CONV, id: "conv_b", title: "Session B" },
    ]);
    renderSidebar();
    const linkA = screen.getByRole("link", { name: /Session A/ });
    const linkB = screen.getByRole("link", { name: /Session B/ });
    const rowA = linkA.closest("li")!;
    const first = touchPoint(linkA, 100, 100);
    const moved = touchPoint(linkA, 100 + ROW_DRAG_ACTIVATE_PX, 100);

    fireEvent.pointerDown(linkA, { ...TOUCH_POINTER, clientX: 100, clientY: 100 });
    fireEvent.touchStart(linkA, { touches: [first], changedTouches: [first] });
    advanceHold();
    fireEvent.pointerMove(linkA, {
      ...TOUCH_POINTER,
      clientX: 100 + ROW_DRAG_ACTIVATE_PX,
      clientY: 100,
    });
    fireEvent.touchMove(linkA, { touches: [moved], changedTouches: [moved] });
    expect(rowA).toHaveClass("opacity-40");

    const second = touchPoint(linkB, 104, 104, 1);
    fireEvent.pointerDown(linkB, { ...SECOND_TOUCH_POINTER, clientX: 104, clientY: 104 });
    fireEvent.touchStart(linkB, {
      touches: [moved, second],
      changedTouches: [second],
    });

    expect(rowA).not.toHaveClass("opacity-40");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("cancels dnd-kit when a second touch joins the active row", () => {
    vi.useFakeTimers();
    mocks.isMobile = true;
    renderSidebar();
    const { link, row } = touchTarget();

    startTouch();
    advanceHold();
    moveTouch(100 + ROW_DRAG_ACTIVATE_PX, 100);
    expect(row).toHaveClass("opacity-40");

    fireEvent.pointerDown(link, { ...SECOND_TOUCH_POINTER, clientX: 104, clientY: 104 });

    expect(row).not.toHaveClass("opacity-40");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  // Chrome samples touch-action at touchstart; the armed-state touch-none
  // class swap can't retroactively stop an in-flight pan. Only a real native
  // non-passive touchmove listener, held from arm to full reset, forestalls it.
  describe("native touchmove guard", () => {
    function nativeTouchMove() {
      const event = new Event("touchmove", { bubbles: true, cancelable: true });
      document.dispatchEvent(event);
      return event;
    }

    it("does not guard before the hold arms", () => {
      vi.useFakeTimers();
      mocks.isMobile = true;
      renderSidebar();
      const addSpy = vi.spyOn(document, "addEventListener");

      startTouch();
      expect(addSpy).not.toHaveBeenCalledWith("touchmove", expect.any(Function), {
        passive: false,
      });
      expect(nativeTouchMove().defaultPrevented).toBe(false);
      endTouch();
      addSpy.mockRestore();
    });

    it("registers the guard exactly when armed and removes it on release", () => {
      vi.useFakeTimers();
      mocks.isMobile = true;
      renderSidebar();
      const addSpy = vi.spyOn(document, "addEventListener");
      const removeSpy = vi.spyOn(document, "removeEventListener");

      startTouch();
      advanceHold();
      expect(addSpy).toHaveBeenCalledWith("touchmove", expect.any(Function), { passive: false });
      const touchmoveAdds = addSpy.mock.calls.filter((call) => call[0] === "touchmove");
      expect(touchmoveAdds).toHaveLength(1);
      expect(removeSpy).not.toHaveBeenCalledWith(
        "touchmove",
        expect.any(Function),
        expect.anything(),
      );

      endTouch();
      expect(removeSpy).toHaveBeenCalledWith("touchmove", expect.any(Function), { passive: false });
      addSpy.mockRestore();
      removeSpy.mockRestore();
    });

    it("preventDefaults a cancelable touchmove while armed and through drag", () => {
      vi.useFakeTimers();
      mocks.isMobile = true;
      renderSidebar();

      startTouch();
      advanceHold();
      expect(nativeTouchMove().defaultPrevented).toBe(true);

      moveTouch(100 + ROW_DRAG_ACTIVATE_PX, 100);
      expect(nativeTouchMove().defaultPrevented).toBe(true);
      endTouch(100 + ROW_DRAG_ACTIVATE_PX, 100);
    });

    it("removes the guard on pointercancel", () => {
      vi.useFakeTimers();
      mocks.isMobile = true;
      renderSidebar();
      const { link } = touchTarget();

      startTouch();
      advanceHold();
      const removeSpy = vi.spyOn(document, "removeEventListener");
      fireEvent.pointerCancel(link, { ...TOUCH_POINTER, clientX: 100, clientY: 100 });
      fireEvent.touchCancel(link, { touches: [], changedTouches: [touchPoint(link, 100, 100)] });

      expect(removeSpy).toHaveBeenCalledWith("touchmove", expect.any(Function), { passive: false });
      expect(nativeTouchMove().defaultPrevented).toBe(false);
      removeSpy.mockRestore();
    });

    it("removes the guard when a second touch cancels the gesture", () => {
      vi.useFakeTimers();
      mocks.isMobile = true;
      renderSidebar();
      const { link } = touchTarget();

      startTouch();
      advanceHold();
      const removeSpy = vi.spyOn(document, "removeEventListener");
      fireEvent.pointerDown(link, { ...SECOND_TOUCH_POINTER, clientX: 104, clientY: 104 });

      expect(removeSpy).toHaveBeenCalledWith("touchmove", expect.any(Function), { passive: false });
      expect(nativeTouchMove().defaultPrevented).toBe(false);
      removeSpy.mockRestore();
    });
  });
});

describe("touch swipe actions", () => {
  beforeEach(() => {
    mocks.hasCoarsePointer = true;
  });

  // jsdom has no real touch, so drive the gesture with pointer events. The row
  // recognizer gates on a primary touch pointer; pair each pointer event with
  // its touch event so the real dnd-kit activator also sees the sequence.

  function swipeRow(dx: number) {
    startTouch();
    // First move locks the axis; the second carries it past the commit point.
    moveTouch(100 + Math.sign(dx) * 20, 100);
    moveTouch(100 + dx, 100);
    endTouch(100 + dx, 100);
  }

  it("runs the archive path when swiping the archive-configured direction", () => {
    // Default: swipe-left → archive. Swipe left past the commit threshold.
    mocks.isMobile = true;
    renderSidebar();

    swipeRow(-90);

    // Archiving is a single PATCH — the server stops the runner once the flag
    // commits, so a client-side stop would race it (see runArchive).
    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
    expect(mocks.stopSession.mutate).not.toHaveBeenCalled();
    // Archive never routes through delete.
    expect(mocks.del.mutate).not.toHaveBeenCalled();
  });

  it("commits on a wide viewport with a coarse pointer", () => {
    mocks.isMobile = false;
    renderSidebar();

    swipeRow(-90);

    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
  });

  it("opens the delete confirm dialog (no immediate delete) when swiping the delete direction", () => {
    // Map swipe-right → delete, then swipe right.
    writeSwipeActions({ left: "archive", right: "delete" });
    mocks.isMobile = true;
    renderSidebar();

    swipeRow(90);

    // The confirm dialog opens — delete stays behind it, so no mutation yet.
    expect(screen.getByText("Delete conversation?")).toBeInTheDocument();
    expect(mocks.del.mutate).not.toHaveBeenCalled();

    // Confirming in the dialog fires the same delete the kebab flow uses.
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mocks.del.mutate).toHaveBeenCalledWith(
      { id: "conv_1", deleteBranch: false },
      expect.anything(),
    );
  });

  it("does nothing when swiping a direction mapped to none", () => {
    // Default: swipe-right → none. Swipe right; nothing should fire.
    mocks.isMobile = true;
    renderSidebar();

    swipeRow(90);

    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    expect(mocks.del.mutate).not.toHaveBeenCalled();
    expect(screen.queryByText("Delete conversation?")).toBeNull();
  });

  it("supports both directions mapped to the same action", () => {
    // Both directions archive — a swipe either way runs the archive path.
    writeSwipeActions({ left: "archive", right: "archive" });
    mocks.isMobile = true;
    renderSidebar();

    swipeRow(90);
    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );

    mocks.archive.mutate.mockClear();
    swipeRow(-90);
    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
  });

  it("commits a fast flick from the release position before React renders", () => {
    mocks.isMobile = true;
    renderSidebar();
    const { link } = touchTarget();
    startTouch(200, 100);

    act(() => {
      for (const x of [180, 100]) {
        const touch = touchPoint(link, x, 100);
        fireEvent.pointerMove(link, { ...TOUCH_POINTER, clientX: x, clientY: 100 });
        fireEvent.touchMove(link, { touches: [touch], changedTouches: [touch] });
      }
      const touch = touchPoint(link, 100, 100);
      fireEvent.pointerUp(link, { ...TOUCH_POINTER, clientX: 100, clientY: 100 });
      fireEvent.touchEnd(link, { touches: [], changedTouches: [touch] });
    });

    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
  });

  it("keeps the committed swipe action when a concurrent render is abandoned", () => {
    mocks.isMobile = true;
    const view = renderSidebar();
    const abandonedArchive = { mutate: vi.fn() };
    const { link } = touchTarget();
    startTouch(200, 100);

    mocks.archiveOverride = abandonedArchive;
    mocks.suspendDraggable = true;
    act(() => {
      startTransition(() => view.rerenderSidebar());
    });
    expect(screen.queryByTestId("suspended-sidebar")).toBeNull();

    for (const x of [180, 100]) {
      const touch = touchPoint(link, x, 100);
      fireEvent.pointerMove(link, { ...TOUCH_POINTER, clientX: x, clientY: 100 });
      fireEvent.touchMove(link, { touches: [touch], changedTouches: [touch] });
    }
    const touch = touchPoint(link, 100, 100);
    fireEvent.pointerUp(link, { ...TOUCH_POINTER, clientX: 100, clientY: 100 });
    fireEvent.touchEnd(link, { touches: [], changedTouches: [touch] });
    mocks.suspendDraggable = false;

    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
    expect(abandonedArchive.mutate).not.toHaveBeenCalled();
  });

  it("does not commit after a flick returns below the threshold before release", () => {
    mocks.isMobile = true;
    renderSidebar();
    const { link } = touchTarget();
    startTouch(200, 100);

    act(() => {
      for (const x of [100, 190]) {
        const touch = touchPoint(link, x, 100);
        fireEvent.pointerMove(link, { ...TOUCH_POINTER, clientX: x, clientY: 100 });
        fireEvent.touchMove(link, { touches: [touch], changedTouches: [touch] });
      }
      const touch = touchPoint(link, 190, 100);
      fireEvent.pointerUp(link, { ...TOUCH_POINTER, clientX: 190, clientY: 100 });
      fireEvent.touchEnd(link, { touches: [], changedTouches: [touch] });
    });

    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    expect(mocks.del.mutate).not.toHaveBeenCalled();
  });

  it("resists travel past commit and caps the row offset", () => {
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();
    const surface = row.querySelector<HTMLElement>("div.relative")!;

    startTouch(400, 100);
    moveTouch(340, 100);
    expect(surface.style.marginRight).toBe("60px");
    moveTouch(100, 100);
    expect(surface.style.marginRight).toBe("96px");
    endTouch(100, 100);

    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
  });

  it("applies one-third resistance immediately past the commit distance", () => {
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();
    const surface = row.querySelector<HTMLElement>("div.relative")!;

    startTouch(100, 100);
    moveTouch(10, 100);

    expect(surface.style.marginRight).toBe("78px");
    endTouch(10, 100);
  });

  it("ignores touch gestures that bubble from a portalled dialog", () => {
    mocks.isMobile = true;
    renderSidebar();
    const { row } = touchTarget();
    const surface = row.querySelector<HTMLElement>("div.relative")!;

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("delete-conversation"));
    const confirm = screen.getByRole("button", { name: "Delete" });
    fireEvent.pointerDown(confirm, { ...TOUCH_POINTER, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(confirm, { ...TOUCH_POINTER, clientX: 10, clientY: 100 });
    fireEvent.pointerUp(confirm, { ...TOUCH_POINTER, clientX: 10, clientY: 100 });

    expect(surface.style.marginLeft).toBe("");
    expect(surface.style.marginRight).toBe("");
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    expect(mocks.del.mutate).not.toHaveBeenCalled();
  });

  it("does not fire the action for a short swipe below the commit threshold", () => {
    mocks.isMobile = true;
    renderSidebar();

    // Past the activation lock (20px) but short of the commit distance (72px).
    swipeRow(-40);
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("commits at exactly 72px but not at 71px", () => {
    mocks.isMobile = true;
    renderSidebar();

    swipeRow(-71);
    expect(mocks.archive.mutate).not.toHaveBeenCalled();

    swipeRow(-72);
    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.anything(),
    );
  });

  it("ignores touch gestures without a coarse pointer", () => {
    vi.useFakeTimers();
    mocks.hasCoarsePointer = false;
    renderSidebar();

    swipeRow(-90);
    expect(mocks.archive.mutate).not.toHaveBeenCalled();

    const { row } = touchTarget();
    startTouch();
    act(() => vi.advanceTimersByTime(ROW_GESTURE_HOLD_MS));
    moveTouch(101, 100);
    expect(row).not.toHaveClass("scale-[1.01]");
    expect(row).not.toHaveClass("opacity-40");
    endTouch(101, 100);
  });

  it("ignores non-touch pointers (pen/stylus/mouse) on mobile", () => {
    // The gesture is touch-only. Swipe LEFT, which is configured to archive, so
    // the direction itself can't be what makes this inert — and assert the row
    // never moves, not just that no mutation fired.
    vi.useFakeTimers();
    writeSwipeActions({ left: "archive", right: "archive" });
    mocks.isMobile = true;
    renderSidebar();

    const link = screen.getByRole("link", { name: /My Session/ });
    const li = link.closest("li")!;
    const surface = li.querySelector<HTMLElement>("div.relative");
    const pen = { pointerId: 1, isPrimary: true, pointerType: "pen" as const };
    fireEvent.pointerDown(li, { ...pen, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(li, { ...pen, clientX: 80, clientY: 100 });
    fireEvent.pointerMove(li, { ...pen, clientX: 10, clientY: 100 });

    // No translate, and holding a pen still never arms the long-press.
    expect(surface?.style.marginRight).toBe("");
    act(() => vi.advanceTimersByTime(ROW_GESTURE_HOLD_MS + 1));
    expect(li).not.toHaveClass("scale-[1.01]");

    fireEvent.pointerUp(li, { ...pen, clientX: 10, clientY: 100 });

    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    expect(mocks.del.mutate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("rename-conversation")).toBeNull();
  });

  it("yields a primarily-vertical drag to scroll (no action)", () => {
    // A mostly-vertical move is scroll intent — the gesture bails and never
    // fires, even though it travels well past the commit distance horizontally.
    mocks.isMobile = true;
    renderSidebar();

    const li = screen.getByRole("link", { name: /My Session/ }).closest("li")!;
    fireEvent.pointerDown(li, { ...TOUCH_POINTER, clientX: 100, clientY: 100 });
    // First move is dominated by the vertical axis → decided="other".
    fireEvent.pointerMove(li, { ...TOUCH_POINTER, clientX: 105, clientY: 140 });
    fireEvent.pointerMove(li, { ...TOUCH_POINTER, clientX: 10, clientY: 180 });
    fireEvent.pointerUp(li, { ...TOUCH_POINTER, clientX: 10, clientY: 180 });

    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    expect(mocks.del.mutate).not.toHaveBeenCalled();
  });
});

describe("sharing kill switch", () => {
  it("disables the row's Share item for a manager when sharing_mode is off", () => {
    // CONV is owner-level (permission_level null → canManage), yet a server
    // reporting sharing_mode off must gray out Share for everyone.
    mockConversations([CONV]);
    renderSidebar(undefined, serverInfo({ sharing_mode: "off" }));

    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));

    // Radix marks a disabled menu item with data-disabled; the enabled
    // (on / read_only) branch renders a plain selectable item without it.
    expect(screen.getByTestId("share-conversation")).toHaveAttribute("data-disabled");
  });

  it("keeps the row's Share item enabled for a manager when sharing is on", () => {
    mockConversations([CONV]);
    renderSidebar(undefined, serverInfo({ sharing_mode: "on" }));

    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));

    expect(screen.getByTestId("share-conversation")).not.toHaveAttribute("data-disabled");
  });

  it("omits the row's Share item entirely in single-user mode", () => {
    // Explicit single_user marker: no other users to share with, so the item
    // is removed — not just disabled like the sharing-off case.
    // isCurrentServerLocal is mocked false, so this exercises the single-user
    // gate specifically (not the local-server path).
    mockConversations([CONV]);
    renderSidebar(undefined, serverInfo({ single_user: true }));

    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));

    expect(screen.queryByTestId("share-conversation")).toBeNull();
    // Other row actions still render — only Share is gated on single-user.
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
  });

  it("keeps the row's Share item on a multi-user header-auth deploy (not single_user)", () => {
    // Header-auth multi-user (SSO proxy): accounts off AND no login_url, same
    // shape as single-user, but single_user false — the item must stay.
    mockConversations([CONV]);
    renderSidebar(undefined, serverInfo({ single_user: false }));

    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));

    expect(screen.getByTestId("share-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("share-conversation")).not.toHaveAttribute("data-disabled");
  });
});
