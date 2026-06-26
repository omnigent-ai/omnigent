// Layout regression tests for the sidebar's bulk-action bar (selection
// mode). The reported bug: on mobile the Archive/Delete buttons floated
// *over* other controls. The cause was that the mobile copy of those
// buttons lived inline in the same flex row as the "Exit selection"
// button, which is absolutely positioned (`absolute right-0`) — so the
// inline buttons overflowed underneath it. The fix removes the duplicated
// mobile-only inline copy and renders the Archive/Delete buttons once, on
// their own row below the count/select-all row, visible at every
// breakpoint. These tests lock that structure in:
//   1. The action buttons sit on a row that does NOT contain the
//      absolutely-positioned Exit button (no overlap).
//   2. That row is not breakpoint-gated (no `hidden`/`md:hidden`) and is
//      in normal flow (not `absolute`), so it shows on mobile.
//   3. The actions render exactly once (no mobile/desktop duplication).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

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
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useSetSessionLabel: () => ({ mutate: vi.fn() }),
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

// Owner (permission_level null), not archived → Archive + Delete both apply.
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
  useConvMock.mockImplementation(() => withData);
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

/** Enter selection mode and select the (single) session so the
 *  Archive/Delete actions are enabled. */
function enterSelectionModeAndSelect() {
  fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
  // In selection mode the row link toggles selection instead of navigating.
  fireEvent.click(screen.getByRole("link", { name: /My Session/ }));
}

beforeEach(() => {
  mockConversations([CONV]);
});

afterEach(() => {
  cleanup();
});

describe("bulk-action bar layout", () => {
  it("renders Archive/Delete on a row separate from the absolutely-positioned Exit button", () => {
    renderSidebar();
    enterSelectionModeAndSelect();

    const exitBtn = screen.getByRole("button", { name: "Exit selection mode" });
    // The exit button is the absolutely-positioned control that the action
    // buttons used to overflow under.
    expect(exitBtn.className).toContain("absolute");

    const deleteBtn = screen.getByTestId("bulk-delete");
    const actionRow = deleteBtn.parentElement as HTMLElement;

    // The fix: the action buttons live on their own row, NOT inside the
    // row that holds the floating Exit button. If they shared a row again,
    // the overlap would return.
    expect(actionRow).not.toContainElement(exitBtn);
    expect(screen.getByTestId("bulk-archive").parentElement).toBe(actionRow);
  });

  it("keeps the action row visible at every breakpoint and in normal flow", () => {
    renderSidebar();
    enterSelectionModeAndSelect();

    const actionRow = screen.getByTestId("bulk-delete").parentElement as HTMLElement;

    // Must not be breakpoint-gated — the old desktop copy was `md:flex`
    // (hidden on mobile) and the mobile copy was the overlapping inline one.
    expect(actionRow.className).not.toMatch(/\bhidden\b/);
    expect(actionRow.className).not.toMatch(/\bmd:hidden\b/);
    // Must stay in normal flow so it can't float over neighbours.
    expect(actionRow.className).not.toMatch(/\babsolute\b/);
  });

  it("renders the Archive and Delete actions exactly once (no mobile/desktop duplication)", () => {
    renderSidebar();
    enterSelectionModeAndSelect();

    // The pre-fix layout shipped two copies (mobile inline + desktop row);
    // there must now be a single instance of each action.
    expect(screen.getAllByRole("button", { name: /^Archive$/ })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /^Delete/ })).toHaveLength(1);
  });
});
