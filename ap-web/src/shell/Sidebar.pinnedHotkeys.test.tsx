// The first ten pinned rows surface their Cmd/Ctrl+digit shortcut as a small
// leading chip (1–9 then 0). Pins beyond ten — and non-pinned rows — get none.
// See ConversationRow's `shortcutDigit` and usePinnedSessionHotkeys.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({ mutate: vi.fn() }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
vi.mock("@/components/theme/ThemeModeMenu", () => ({ ThemeModeMenu: () => null }));

// The chips are desktop-only; default the shell to Electron and flip per-test.
const isNativeShell = vi.fn(() => true);
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/nativeBridge")>()),
  isNativeShell: () => isNativeShell(),
}));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { PINNED_CONVERSATION_IDS_STORAGE_KEY } from "./sidebarNav";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

// id pN sorts to pinned position N (updated_at descending).
function pinnedConv(n: number, total: number): Conversation {
  return {
    id: `p${n}`,
    object: "conversation",
    title: `Pinned ${n}`,
    created_at: 0,
    updated_at: total - n,
    labels: {},
    permission_level: null,
  };
}

function mockConversations(conversations: Conversation[]) {
  useConvMock.mockImplementation(
    () =>
      ({
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
      }) as unknown as ReturnType<typeof useConversations>,
  );
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useConvMock.mockReset();
  isNativeShell.mockReturnValue(true);
  localStorage.clear();
});
afterEach(cleanup);

describe("pinned-session shortcut chips", () => {
  it("shows a digit chip on the first ten pinned rows (1–9 then 0), none on the 11th", () => {
    const eleven = Array.from({ length: 11 }, (_, i) => pinnedConv(i, 11));
    mockConversations(eleven);
    localStorage.setItem(
      PINNED_CONVERSATION_IDS_STORAGE_KEY,
      JSON.stringify(eleven.map((c) => c.id)),
    );
    renderSidebar();

    const chips = screen.getAllByTestId("pinned-shortcut-hint");
    expect(chips).toHaveLength(10);
    expect(chips.map((c) => c.textContent?.replace(/[^0-9]/g, ""))).toEqual([
      "1",
      "2",
      "3",
      "4",
      "5",
      "6",
      "7",
      "8",
      "9",
      "0",
    ]);

    // The 11th pinned row exists but carries no shortcut chip.
    const eleventh = screen.getByText("Pinned 10").closest("li");
    expect(eleventh).not.toBeNull();
    expect(within(eleventh as HTMLElement).queryByTestId("pinned-shortcut-hint")).toBeNull();
  });

  it("shows no chips when nothing is pinned", () => {
    const rows = [pinnedConv(0, 3), pinnedConv(1, 3), pinnedConv(2, 3)];
    mockConversations(rows);
    renderSidebar();
    expect(screen.queryByTestId("pinned-shortcut-hint")).toBeNull();
  });

  it("shows no chips in a plain browser (not the Electron shell)", () => {
    isNativeShell.mockReturnValue(false);
    const three = [pinnedConv(0, 3), pinnedConv(1, 3), pinnedConv(2, 3)];
    mockConversations(three);
    localStorage.setItem(
      PINNED_CONVERSATION_IDS_STORAGE_KEY,
      JSON.stringify(three.map((c) => c.id)),
    );
    renderSidebar();
    expect(screen.queryByTestId("pinned-shortcut-hint")).toBeNull();
  });
});
