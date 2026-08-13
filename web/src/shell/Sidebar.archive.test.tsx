// Tests for the archive flow in the sidebar. Contract: archiving sends ONLY
// the archive PATCH (`archived: true`). The "Archiving…" status row stays up
// until the row LEAVES the sidebar — on success it's held (the row unmounts
// when the list refetch drops the archived row, taking the spinner with it),
// and only an error clears it to restore the interactive row for retry.
// Clearing on settle would flash the row back to its clickable form a
// round-trip before it actually leaves. The runner stop is the server's job
// once the flag commits — a client stop would race the server's against the
// same runner, and it would also put the runner's stop timeouts in front of
// the flag flip. The kebab's user-facing "Stop session" action is a separate
// affordance covered by Sidebar.stop.test.tsx. Unarchiving flips the flag
// back with no status row. See ConversationRow.runArchive in Sidebar.tsx.
//
// Archived sessions are no longer listed in the sidebar (they moved to the
// Settings page), so unarchiving is covered by SettingsPage.test.tsx; this
// file exercises the archive path from a row's kebab.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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

/** Open the row's action dropdown and click the archive/unarchive item. */
function clickArchive() {
  // Radix DropdownMenu opens on pointerdown, not click.
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
  fireEvent.click(screen.getByTestId("archive-conversation"));
}

beforeEach(() => {
  mocks.archive.mutate.mockReset();
  mocks.stop.mutate.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("archive flow", () => {
  it("archives with a single PATCH and no client-side stop", () => {
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    expect(mocks.archive.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.objectContaining({
        onSuccess: expect.any(Function),
        // Only an error restores the interactive row; success intentionally
        // leaves the spinner up until the row unmounts (see the "keeps the
        // 'Archiving…' row up after the archive succeeds" case below).
        onError: expect.any(Function),
      }),
    );
    // The server owns the stop. A client stop here would race it against
    // the same runner and put its timeouts in front of the flag flip.
    expect(mocks.stop.mutate).not.toHaveBeenCalled();
  });

  it("toasts a pointer to Settings once the archive succeeds", () => {
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    // Drive the archive to its success callback.
    const archiveArgs = mocks.archive.mutate.mock.calls[0];
    act(() => (archiveArgs[1] as { onSuccess: () => void }).onSuccess());

    const toast = screen.getByTestId("toast");
    expect(within(toast).getByText(/View archived sessions in/)).toBeInTheDocument();
    expect(within(toast).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/settings/archived",
    );
  });

  // Unarchive moved out of the sidebar: archived sessions no longer render
  // here (they're on the Settings page), so the "Unarchive" affordance is
  // covered by SettingsPage.test.tsx instead.

  it("shows an 'Archiving…' status row while the archive is in flight", () => {
    // The archive mock never settles (vi.fn() stub), so the row stays in
    // its in-flight state — the window the user sees. Without the indicator
    // the row would look idle while the archive ran.
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    const row = screen.getByTestId("conversation-archiving");
    expect(within(row).getByText("Archiving…")).toBeInTheDocument();
    // The interactive link is replaced by the status row, so the session
    // can't be re-opened or re-archived mid-flight.
    expect(screen.queryByRole("link", { name: /My Session/ })).not.toBeInTheDocument();
  });

  it("keeps the 'Archiving…' row up after the archive succeeds", () => {
    // On success the spinner must NOT be cleared: the row leaves the sidebar
    // only when the ['conversations'] refetch drops the archived row (which
    // unmounts this row and its spinner together). Clearing on success would
    // flash the row back to its plain, clickable form for the round-trip until
    // the refetch lands — the exact regression this guards. Here the mocked
    // list still contains the row (no refetch modeled), so firing onSuccess
    // must leave the status row in place.
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    expect(screen.getByTestId("conversation-archiving")).toBeInTheDocument();

    const archiveOnSuccess = mocks.archive.mutate.mock.calls[0][1].onSuccess as () => void;
    act(() => archiveOnSuccess());

    // Still archiving, still no interactive link — the row hasn't left yet.
    expect(screen.getByTestId("conversation-archiving")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /My Session/ })).not.toBeInTheDocument();
  });

  it("restores the interactive row when the archive errors", () => {
    // The only escape hatch from the status row: a failed archive clears the
    // flag so the user gets the clickable row back to retry. Success has no
    // such clear (see above) — the row instead leaves via the refetch.
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    expect(screen.getByTestId("conversation-archiving")).toBeInTheDocument();

    const archiveOnError = mocks.archive.mutate.mock.calls[0][1].onError as () => void;
    act(() => archiveOnError());

    expect(screen.queryByTestId("conversation-archiving")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /My Session/ })).toBeInTheDocument();
  });
});
