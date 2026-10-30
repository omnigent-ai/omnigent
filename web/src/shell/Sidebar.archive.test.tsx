import { conversationPage } from "@/test/sidebarMockHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// Tests for the archive flow in the sidebar. Contract: archiving sends ONLY
// the archive PATCH (`archived: true`) — no client stop. The row leaves the
// sidebar optimistically (useArchiveConversation flips the cached `archived`
// flag in onMutate; the list filters archived rows out client-side), so there
// is no "Archiving…" status row — the row simply unmounts, like delete's. The
// only synchronous side effect is the Undo pill (which also links to Settings).
// The runner stop is the server's job once the flag commits — a client stop
// would race the server's against the same runner, and put the runner's stop
// timeouts in front of the flag flip. The kebab's user-facing "Stop session"
// action is a separate affordance covered by Sidebar.stop.test.tsx.
// The optimistic cache overlay + error reconcile is covered in
// sessionListCache.test.ts / useConversations.test.ts.
//
// Archived sessions are no longer listed in the sidebar (they moved to the
// Settings page), so unarchiving is covered by SettingsPage.test.tsx; this
// file exercises the archive path from a row's kebab.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import * as identity from "@/lib/identity";
import type * as RelatedSessionLifecycle from "@/hooks/useRelatedSessionLifecycle";

// Controllable archive + stop mutations, declared via vi.hoisted so the
// vi.mock factory can reference them.
const mocks = vi.hoisted(() => ({
  archive: { mutate: vi.fn() },
  bulkArchive: { mutate: vi.fn() },
  stop: { mutate: vi.fn() },
  archiveGroup: { mutate: vi.fn(), isPending: false },
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
vi.mock("@/hooks/useRelatedSessionLifecycle", async (importOriginal) => ({
  ...(await importOriginal<typeof RelatedSessionLifecycle>()),
  useArchiveRelatedSessions: () => mocks.archiveGroup,
}));

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

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname}
      {location.search}
    </output>
  );
}

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
              <LocationProbe />
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
  mocks.archiveGroup.mutate.mockReset();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
});

it("offers Add agent and All lifecycle actions for a group containing an archived peer", async () => {
  const fetch = vi.spyOn(identity, "authenticatedFetch").mockImplementation(
    async () =>
      new Response(
        JSON.stringify({
          data: [{ id: "peer", archived: true, labels: { "omnigent.session_role": "peer" } }],
          has_more: false,
          hosts: [],
          order: [],
        }),
      ),
  );
  mockConversations([CONV]);
  renderSidebar({ ...CLEANUP_SERVER, features: { related_sessions: true } });
  expect(fetch.mock.calls.some(([url]) => String(url).includes("child_sessions"))).toBe(false);
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
  await waitFor(() =>
    expect(screen.getByTestId("archive-conversation")).toHaveTextContent("Archive All"),
  );
  expect(screen.getByTestId("delete-conversation")).toHaveTextContent("Delete All");
  fireEvent.click(screen.getByTestId("archive-conversation"));
  expect(mocks.archiveGroup.mutate).toHaveBeenCalledWith({ conversation: CONV, archived: true });
  expect(mocks.archive.mutate).not.toHaveBeenCalled();
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
  fireEvent.click(screen.getByTestId("add-agent-conversation"));
  expect(screen.getByTestId("location")).toHaveTextContent("/c/conv_1?panel=agents&newSession=1");
});

it("keeps helpers on the legacy archive path and hides new actions on older servers", async () => {
  vi.spyOn(identity, "authenticatedFetch").mockImplementation(
    async () =>
      new Response(
        JSON.stringify({
          data: [{ id: "helper", labels: {} }],
          has_more: false,
          hosts: [],
          order: [],
        }),
      ),
  );
  mockConversations([CONV]);
  const view = renderSidebar({ ...CLEANUP_SERVER, features: { related_sessions: true } });
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
  await waitFor(() =>
    expect(screen.getByTestId("archive-conversation")).not.toHaveAttribute("data-disabled"),
  );
  expect(screen.getByTestId("archive-conversation")).toHaveTextContent(/^Archive$/);
  fireEvent.click(screen.getByTestId("archive-conversation"));
  expect(mocks.archive.mutate).toHaveBeenCalled();
  expect(mocks.archiveGroup.mutate).not.toHaveBeenCalled();
  view.unmount();
  renderSidebar(FALLBACK_SERVER_INFO);
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
  expect(screen.queryByTestId("add-agent-conversation")).not.toBeInTheDocument();
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

  it.each([null, "true"])(
    "keeps bulk-archived worktrees with related sessions enabled and cleanup preference %s",
    async (preference) => {
      if (preference) localStorage.setItem(PREF_KEY, preference);
      mockConversations([WORKTREE_CONV]);
      renderSidebar({ ...CLEANUP_SERVER, features: { related_sessions: true } });
      fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
      fireEvent.click(screen.getByRole("link", { name: /My Session/ }));
      fireEvent.click(screen.getByTestId("bulk-archive"));

      expect(screen.queryByTestId("archive-worktree-dialog")).not.toBeInTheDocument();
      expect(mocks.bulkArchive.mutate).toHaveBeenCalledWith({
        ids: ["conv_1"],
        archived: true,
        deleteWorktreeIds: new Set(),
        onArchived: expect.any(Function),
      });
      expect(
        await screen.findByText("Worktrees will be kept when archiving these sessions."),
      ).toBeInTheDocument();
    },
  );

  it("keeps the bulk cleanup prompt when related sessions are disabled", async () => {
    mockConversations([WORKTREE_CONV]);
    renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
    fireEvent.click(screen.getByRole("link", { name: /My Session/ }));
    fireEvent.click(screen.getByTestId("bulk-archive"));

    await screen.findByTestId("archive-worktree-dialog");
    expect(mocks.bulkArchive.mutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("archive-worktree-delete"));
    expect(mocks.bulkArchive.mutate).toHaveBeenCalledWith({
      ids: ["conv_1"],
      archived: true,
      deleteWorktreeIds: new Set(["conv_1"]),
    });
  });

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
