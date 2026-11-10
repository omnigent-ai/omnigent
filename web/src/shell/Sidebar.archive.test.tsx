import { conversationPage } from "@/test/sidebarMockHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// Archiving from a row sends only the PATCH (the server stops the runner) and
// the row leaves optimistically, except the open session's (Sidebar.openSession.test.tsx).
// Settings unarchive: SettingsPage.test.tsx; cache overlays: useConversations.test.ts.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";

// Controllable archive + stop mutations, declared via vi.hoisted so the
// vi.mock factory can reference them.
const mocks = vi.hoisted(() => ({
  archive: { mutate: vi.fn() },
  bulkArchive: { mutate: vi.fn() },
  stop: { mutate: vi.fn() },
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useArchiveConversation: () => mocks.archive,
    useBulkArchiveConversations: () => mocks.bulkArchive,
    useStopSession: () => mocks.stop,
  };
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Toaster } from "@/components/ui/sonner";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

// Owner (permission_level null) → archivable.
const CONV: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "My Session",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: { "omnigent.wrapper": "claude-code-native-ui" },
  permission_level: null,
  status: "idle",
};

function mockConversations(conversations: Conversation[]) {
  const result = conversationPage(conversations);
  useConvMock.mockImplementation(() => result);
}

const CLEANUP_SERVER: ServerInfo = { ...FALLBACK_SERVER_INFO, archive_worktree_cleanup: true };

function renderSidebar(serverInfo: ServerInfo = CLEANUP_SERVER) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CapabilitiesProvider info={serverInfo}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/"]}>
              <Sidebar open={true} onClose={vi.fn()} />
              <Toaster />
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

/** Open the row's action dropdown and click the archive/unarchive item. */
function clickArchive() {
  // Radix DropdownMenu opens on pointerdown, not click.
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
  fireEvent.click(screen.getByTestId("archive-conversation"));
}

beforeEach(() => {
  mocks.archive.mutate.mockReset();
  mocks.bulkArchive.mutate.mockReset();
  mocks.stop.mutate.mockReset();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("archive flow", () => {
  it("archives with a single PATCH and no client-side stop", () => {
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    expect(mocks.archive.mutate).toHaveBeenCalledTimes(1);
    // Just the flag — the optimistic overlay + error reconcile live in the
    // hook, and the toast fires synchronously (not in a mutate callback, which
    // wouldn't fire once the optimistic overlay unmounts the row).
    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      deleteWorktree: false,
    });
    // The server owns the stop. A client stop here would race it against
    // the same runner and put its timeouts in front of the flag flip.
    expect(mocks.stop.mutate).not.toHaveBeenCalled();
  });

  it("does not show an 'Archiving…' status row — the row leaves optimistically", () => {
    // No spinner: the cached `archived` flag flips in onMutate and the row
    // unmounts, like delete. (The mocked mutate doesn't model the overlay, so
    // the interactive row is still here — the point is only that no status row
    // replaced it.)
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    expect(screen.queryByTestId("conversation-archiving")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /My Session/ })).toBeInTheDocument();
  });

  it("shows an Undo toast (with a View archived action) on archive", async () => {
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    // The toast fires synchronously on click (the row is about to unmount, so
    // it can't wait for a mutate callback) — no need to drive onSuccess.
    // Singular copy for one session — never "session(s)".
    expect(await screen.findByText("Archived 1 session")).toBeInTheDocument();
    expect(screen.queryByText(/session\(s\)/)).not.toBeInTheDocument();
    // Undo and the View archived pointer are the toast's two actions.
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View archived" })).toBeInTheDocument();
  });

  it("archives from the row's quick-archive hover button", () => {
    mockConversations([CONV]);
    renderSidebar();

    fireEvent.click(screen.getByTestId("quick-archive-conversation"));

    // Same single-PATCH contract as the kebab item, just a different affordance.
    // An idle session skips the running-session confirmation.
    expect(screen.queryByTestId("archive-running-dialog")).not.toBeInTheDocument();
    expect(mocks.archive.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      deleteWorktree: false,
    });
    expect(mocks.stop.mutate).not.toHaveBeenCalled();
  });

  it("unarchives from the quick button on an archived row", () => {
    // Archived rows render under the "Archived sessions" filter; the quick
    // button flips to its unarchive affordance there.
    mockConversations([{ ...CONV, archived: true }]);
    renderSidebar();
    // Radix menu opens on pointerdown; pick the Archived filter.
    fireEvent.pointerDown(screen.getByTestId("session-filter"), { button: 0 });
    fireEvent.click(screen.getByTestId("session-filter-archived"));

    fireEvent.click(screen.getByTestId("quick-archive-conversation"));

    expect(mocks.archive.mutate).toHaveBeenCalledWith({ id: "conv_1", archived: false });
  });
});

describe("archive worktree prompt", () => {
  const WORKTREE_CONV: Conversation = { ...CONV, git_branch: "feature/x" };
  const PREF_KEY = "omnigent:delete-worktrees-on-archive";

  it("archives a non-worktree session without prompting", () => {
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    expect(screen.queryByTestId("archive-worktree-dialog")).not.toBeInTheDocument();
    expect(mocks.archive.mutate).toHaveBeenCalledTimes(1);
  });

  it("asks before archiving a worktree session, then deletes on Yes", async () => {
    mockConversations([WORKTREE_CONV]);
    renderSidebar();
    clickArchive();

    await screen.findByTestId("archive-worktree-dialog");
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("archive-worktree-delete"));

    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      deleteWorktree: true,
    });
    // Without "Remember my choice" the answer isn't remembered.
    expect(localStorage.getItem(PREF_KEY)).toBeNull();
  });

  it("archives only on No and remembers the choice when asked to", async () => {
    mockConversations([WORKTREE_CONV]);
    renderSidebar();
    clickArchive();

    await screen.findByTestId("archive-worktree-dialog");
    fireEvent.click(screen.getByTestId("archive-worktree-remember"));
    fireEvent.click(screen.getByTestId("archive-worktree-keep"));

    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      deleteWorktree: false,
    });
    expect(localStorage.getItem(PREF_KEY)).toBe("false");
  });

  it("cancels the archive when the prompt is dismissed", async () => {
    mockConversations([WORKTREE_CONV]);
    renderSidebar();
    clickArchive();

    const dialog = await screen.findByTestId("archive-worktree-dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(screen.queryByTestId("archive-worktree-dialog")).not.toBeInTheDocument();
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("archives only, without prompting, on a server without worktree cleanup", () => {
    // Older servers reject the unknown `delete_worktree` field, so even a saved
    // opt-in must not send it.
    localStorage.setItem(PREF_KEY, "true");
    mockConversations([WORKTREE_CONV]);
    renderSidebar(FALLBACK_SERVER_INFO);
    clickArchive();

    expect(screen.queryByTestId("archive-worktree-dialog")).not.toBeInTheDocument();
    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      deleteWorktree: false,
    });
  });

  it("applies a saved preference without prompting", () => {
    localStorage.setItem(PREF_KEY, "true");
    mockConversations([WORKTREE_CONV]);
    renderSidebar();
    clickArchive();

    expect(screen.queryByTestId("archive-worktree-dialog")).not.toBeInTheDocument();
    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      deleteWorktree: true,
    });
  });
});

describe("archive running-session prompt", () => {
  const RUNNING_CONV: Conversation = { ...CONV, status: "running" };

  it("asks before archiving a running session from the quick button, and Cancel keeps it", async () => {
    mockConversations([RUNNING_CONV]);
    renderSidebar();
    fireEvent.click(screen.getByTestId("quick-archive-conversation"));

    const dialog = await screen.findByTestId("archive-running-dialog");
    expect(dialog).toHaveTextContent("Archive running session?");
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("archive-running-cancel"));

    expect(screen.queryByTestId("archive-running-dialog")).not.toBeInTheDocument();
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: /My Session/ })).toBeInTheDocument();
  });

  it("archives a running session from the row menu once confirmed", async () => {
    mockConversations([RUNNING_CONV]);
    renderSidebar();
    clickArchive();

    await screen.findByTestId("archive-running-dialog");
    fireEvent.click(screen.getByTestId("archive-running-confirm"));

    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      deleteWorktree: false,
    });
    expect(await screen.findByText("Archived 1 session")).toBeInTheDocument();
  });

  it("cancels the archive when the running prompt is dismissed", async () => {
    mockConversations([RUNNING_CONV]);
    renderSidebar();
    clickArchive();

    const dialog = await screen.findByTestId("archive-running-dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(screen.queryByTestId("archive-running-dialog")).not.toBeInTheDocument();
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("continues to the worktree prompt after confirming a running worktree session", async () => {
    mockConversations([{ ...RUNNING_CONV, git_branch: "feature/x" }]);
    renderSidebar();
    clickArchive();

    fireEvent.click(await screen.findByTestId("archive-running-confirm"));
    await screen.findByTestId("archive-worktree-dialog");
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("archive-worktree-delete"));

    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      deleteWorktree: true,
    });
  });

  it("asks once for a bulk archive that includes a running session", async () => {
    mockConversations([
      { ...CONV, id: "conv_idle", title: "Idle Session" },
      { ...RUNNING_CONV, id: "conv_busy", title: "Busy Session" },
    ]);
    renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
    fireEvent.click(screen.getByRole("link", { name: /Idle Session/ }));
    fireEvent.click(screen.getByRole("link", { name: /Busy Session/ }));
    fireEvent.click(screen.getByTestId("bulk-archive"));

    const dialog = await screen.findByTestId("archive-running-dialog");
    expect(dialog).toHaveTextContent("Archive a running session?");
    expect(dialog).toHaveTextContent("1 of the 2 sessions you're archiving has an agent");
    expect(mocks.bulkArchive.mutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("archive-running-confirm"));

    expect(mocks.bulkArchive.mutate).toHaveBeenCalledTimes(1);
    const [{ ids, archived }] = mocks.bulkArchive.mutate.mock.calls[0];
    expect(archived).toBe(true);
    expect([...ids].sort()).toEqual(["conv_busy", "conv_idle"]);
  });
});

describe("session row quick actions setting", () => {
  it("keeps Pin and Archive in the row menu only when the quick buttons are turned off", () => {
    localStorage.setItem("omnigent:session-row-quick-actions", "false");
    mockConversations([CONV]);
    renderSidebar();

    expect(screen.queryByTestId("quick-pin-conversation")).not.toBeInTheDocument();
    expect(screen.queryByTestId("quick-archive-conversation")).not.toBeInTheDocument();
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    expect(screen.getByTestId("archive-conversation")).toBeInTheDocument();
    // Desktop hides the menu's Pin only while the hover button stands in for it.
    expect(screen.getByTestId("pin-conversation")).not.toHaveClass("md:hidden");
  });
});
