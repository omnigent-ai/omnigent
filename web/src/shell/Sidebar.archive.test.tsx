// Tests for the archive flow in the sidebar. Contract: archiving runs
// stop→archive — it stops the runner first (best-effort resource hygiene,
// NOT the user-facing Stop action, which is the kebab's own "Stop session"
// item covered by Sidebar.stop.test.tsx) and then fires
// `useArchiveConversation` with `archived: true` (with an onSettled that
// clears the "Archiving…" status row). Unarchiving flips the flag back with
// no stop and no status row. See ConversationRow.runArchive in Sidebar.tsx.
//
// Archived sessions are no longer listed in the sidebar (they moved to the
// Settings page), so unarchiving is covered by SettingsPage.test.tsx; this
// file exercises the archive (stop→archive) path from a row's kebab.

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
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => mocks.archive,
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => mocks.stop,
  useProjects: () => ({ data: [] }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
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
  it("archives via stop→archive: stops the runner first, then flips the flag", () => {
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    // Stop fires first (best-effort runner teardown) with the row's id.
    expect(mocks.stop.mutate).toHaveBeenCalledTimes(1);
    const stopArgs = mocks.stop.mutate.mock.calls[0];
    expect(stopArgs[0]).toBe("conv_1");
    // Archive waits for the stop to settle — it hasn't fired yet.
    expect(mocks.archive.mutate).not.toHaveBeenCalled();

    // Settle the stop → archive fires with archived:true + an onSettled
    // that clears the "Archiving…" flag.
    act(() => (stopArgs[1] as { onSettled: () => void }).onSettled());
    expect(mocks.archive.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.archive.mutate).toHaveBeenCalledWith(
      { id: "conv_1", archived: true },
      expect.objectContaining({
        onSuccess: expect.any(Function),
        onSettled: expect.any(Function),
      }),
    );
  });

  it("toasts a pointer to Settings once the archive succeeds", () => {
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    // Drive stop→archive to the success callback.
    const stopArgs = mocks.stop.mutate.mock.calls[0];
    act(() => (stopArgs[1] as { onSettled: () => void }).onSettled());
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
    // The stop mock never settles (vi.fn() stub), so the row stays in its
    // in-flight state — the window the user sees. Without the indicator the
    // row would look idle while the stop→archive ran.
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    const row = screen.getByTestId("conversation-archiving");
    expect(within(row).getByText("Archiving…")).toBeInTheDocument();
    // The interactive link is replaced by the status row, so the session
    // can't be re-opened or re-archived mid-flight (mirrors Deleting…).
    expect(screen.queryByRole("link", { name: /My Session/ })).not.toBeInTheDocument();
  });

  it("clears the 'Archiving…' row once stop→archive settles", () => {
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    expect(screen.getByTestId("conversation-archiving")).toBeInTheDocument();

    // Settle the stop → archive fires; then settle the archive → the row
    // returns to its interactive state (onSettled runs on success or error).
    const stopOnSettled = mocks.stop.mutate.mock.calls[0][1].onSettled as () => void;
    act(() => stopOnSettled());
    const archiveOnSettled = mocks.archive.mutate.mock.calls[0][1].onSettled as () => void;
    act(() => archiveOnSettled());

    expect(screen.queryByTestId("conversation-archiving")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /My Session/ })).toBeInTheDocument();
  });
});
