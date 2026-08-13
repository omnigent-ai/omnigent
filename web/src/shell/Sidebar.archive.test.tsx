// Tests for the archive flow in the sidebar. Contract: clicking Archive
// fires `useArchiveConversation` immediately — no client-side stop first
// (the server stops the runner itself on archive) and no "Archiving…"
// status row, because the mutation is optimistic: the hook's cache overlay
// drops the row from the sidebar on the next frame (covered by the hook
// tests in useConversations.test.ts; here the hook is mocked, so the row
// stays). That unmount also means nothing can wait on a mutate-level
// callback — the toast pointing at the session's new home and the
// navigate-away run synchronously at click time, while the failure toast
// lives in the hook. See ConversationRow.runArchive in Sidebar.tsx.
//
// Archived sessions are no longer listed in the sidebar (they moved to the
// Settings page), so unarchiving is covered by SettingsPage.test.tsx; this
// file exercises the archive path from a row's kebab.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

// Controllable archive + stop mutations, declared via vi.hoisted so the
// vi.mock factory can reference them.
const mocks = vi.hoisted(() => ({
  archive: { mutate: vi.fn() },
  stop: { mutate: vi.fn() },
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useLeaveSession: () => ({ mutate: vi.fn(), isPending: false }),
  useArchiveConversation: () => mocks.archive,
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkMoveToProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => mocks.stop,
  useProjects: () => ({ data: [] }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useProjectConfig: () => ({ data: undefined, isLoading: false }),
  useUpdateProjectConfig: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Toaster } from "@/components/ui/toast";
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
  const withData = {
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
  // The sidebar fetches a single undifferentiated session list, so the
  // mock returns the same data for the one query the component issues.
  useConvMock.mockImplementation(() => withData);
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open={true} onClose={vi.fn()} />
          <Toaster />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

/** Open the row's action dropdown and click the archive item. */
function clickArchive(row?: HTMLElement) {
  const scope = row ? within(row) : screen;
  // Radix DropdownMenu opens on pointerdown, not click.
  fireEvent.pointerDown(scope.getByTestId("conversation-actions"), { button: 0 });
  fireEvent.click(screen.getByTestId("archive-conversation"));
}

beforeEach(() => {
  mocks.archive.mutate.mockReset();
  mocks.stop.mutate.mockReset();
  mockConversations([CONV]);
});

afterEach(() => {
  cleanup();
});

describe("archive flow", () => {
  it("fires the archive mutation immediately — no client-side stop first", () => {
    renderSidebar();
    clickArchive();

    expect(mocks.archive.mutate).toHaveBeenCalledTimes(1);
    // Exactly one argument — no mutate-level callbacks. The hook's overlay
    // unmounts this row on the next frame, and callbacks on an unmounted
    // observer never fire, so everything lives in the hook or runs
    // synchronously at click time (like confirmDelete).
    expect(mocks.archive.mutate).toHaveBeenCalledWith({ id: "conv_1", archived: true });
    // The server stops the runner itself once the archived flag commits,
    // so the client must not gate the archive on a stop round-trip — that
    // serialization was what made archiving feel slow.
    expect(mocks.stop.mutate).not.toHaveBeenCalled();
  });

  it("shows no 'Archiving…' status row — the optimistic overlay removes the row", () => {
    renderSidebar();
    clickArchive();

    // With the hook mocked the row stays rendered, but the old in-flight
    // status row is gone for good: the real hook's cache overlay is what
    // takes the row out of the list now.
    expect(screen.queryByTestId("conversation-archiving")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /My Session/ })).toBeInTheDocument();
  });

  it("toasts a pointer to the session's new home at click time", () => {
    renderSidebar();
    clickArchive();

    // The mutate stub never resolves, so the toast can't be waiting on the
    // server: the row leaves the sidebar immediately and the user needs to
    // know where it went.
    const toast = screen.getByTestId("toast");
    expect(within(toast).getByText(/View archived sessions in/)).toBeInTheDocument();
    expect(within(toast).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/settings/archived",
    );
  });
});

// --- Active-session redirect on archive -------------------------------
//
// The row leaves the sidebar the moment Archive is clicked, so the
// redirect runs then too: if the user is viewing the session being
// archived, the chat surface would otherwise sit on a session that just
// left their list. Archiving any other row must leave them where they
// are. Mirrors the delete flow's redirect tests.

const CONV_OTHER: Conversation = { ...CONV, id: "conv_2", title: "Other Session" };

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname}</div>;
}

/** Render the sidebar inside a router started at `path`, with a probe. */
function renderSidebarRouted(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (
    <>
      <Sidebar open={true} onClose={vi.fn()} />
      <LocationProbe />
    </>
  );
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/" element={tree} />
            <Route path="/c/:conversationId" element={tree} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("active-session redirect on archive", () => {
  it("redirects to / when the archived conversation is the active one", () => {
    mockConversations([CONV, CONV_OTHER]);
    renderSidebarRouted("/c/conv_1");
    expect(screen.getByTestId("loc")).toHaveTextContent("/c/conv_1");

    clickArchive(screen.getByRole("link", { name: /My Session/ }).closest("li") as HTMLElement);

    // Viewing conv_1 when it was archived → bounce to / immediately. The
    // mutate stub never resolves, proving the redirect doesn't wait on it.
    expect(screen.getByTestId("loc")).toHaveTextContent("/");
  });

  it("does NOT redirect when archiving a row the user isn't viewing", () => {
    mockConversations([CONV, CONV_OTHER]);
    renderSidebarRouted("/c/conv_2");
    expect(screen.getByTestId("loc")).toHaveTextContent("/c/conv_2");

    clickArchive(screen.getByRole("link", { name: /My Session/ }).closest("li") as HTMLElement);

    // conv_2 is untouched, so the user stays in the chat they're reading —
    // a redirect here would yank them out of an unrelated session.
    expect(screen.getByTestId("loc")).toHaveTextContent("/c/conv_2");
  });
});
