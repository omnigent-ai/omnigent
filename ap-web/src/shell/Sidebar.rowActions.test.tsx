// Tests for the sidebar conversation-row quick actions:
//   1. A pin/unpin button (the sole pin affordance — no longer in the kebab
//      menu) that toggles the pin (ConversationRow's `quick-pin-conversation`).
//   2. Double-clicking a row to enter inline rename (ConversationRow's
//      `onDoubleClick`), gated on edit permission.
// See ConversationRow / ConversationEditRow in Sidebar.tsx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

// Controllable rename mutation so the double-click test can assert the
// committed title was forwarded to the PATCH. Declared via vi.hoisted so the
// vi.mock factory (hoisted above imports) can reference it.
const mocks = vi.hoisted(() => ({
  rename: { mutate: vi.fn() },
  copyText: vi.fn(() => Promise.resolve()),
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
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => mocks.rename,
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useStopSession: () => ({ mutate: vi.fn() }),
}));

// Heavy sibling widgets pull their own hooks/providers; stub them so this
// test stays scoped to the conversation row.
vi.mock("./AgentTypeFilter", () => ({ AgentTypeFilter: () => null }));
vi.mock("./ReportIssueButton", () => ({ ReportIssueButton: () => null }));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
vi.mock("@/components/theme/ThemeModeMenu", () => ({ ThemeModeMenu: () => null }));
vi.mock("@/lib/clipboard", () => ({ copyText: mocks.copyText }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

const CONV: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "My Session",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: {},
  permission_level: null, // owner → can edit + pin
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

beforeEach(() => {
  mocks.rename.mutate.mockReset();
  mocks.copyText.mockClear();
  useConvMock.mockReset();
  localStorage.clear();
  mockConversations([CONV]);
});

afterEach(cleanup);

describe("quick pin/unpin hover button", () => {
  it("shows copy session id below rename in the wider row action menu", () => {
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });

    const menu = screen.getByRole("menu");
    expect(menu.className).toContain("min-w-44");
    const renameItem = screen.getByTestId("rename-conversation");
    const copyItem = screen.getByTestId("copy-conversation-id");
    expect(
      Boolean(renameItem.compareDocumentPosition(copyItem) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);

    fireEvent.click(screen.getByTestId("copy-conversation-id"));

    expect(mocks.copyText).toHaveBeenCalledTimes(1);
    expect(mocks.copyText).toHaveBeenCalledWith("conv_1");
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

    // Persisted to localStorage so the pin survives a reload (same contract
    // as the kebab's Pin item).
    expect(localStorage.getItem("omnigent:pinned-conversation-ids")).toContain("conv_1");

    // Clicking again unpins: the Pinned section disappears.
    fireEvent.click(screen.getByTestId("quick-pin-conversation"));
    expect(screen.queryByText("Pinned")).toBeNull();
  });

  it("no longer offers Pin in the kebab menu (the quick button replaced it)", () => {
    renderSidebar();

    // Radix DropdownMenu opens on pointerdown, not click.
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });

    // The menu opened (a sibling item is present) but the old Pin item is gone
    // — pinning now lives only on the hover/quick button.
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(screen.queryByTestId("pin-conversation")).toBeNull();
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

  it("does not enter rename on double-click for a viewer-only row", () => {
    // permission_level 1 is below the edit threshold (>= 2), so the kebab's
    // Rename item is disabled and double-click must be inert too.
    mockConversations([{ ...CONV, permission_level: 1 }]);
    renderSidebar();

    fireEvent.dblClick(screen.getByRole("link", { name: /My Session/ }));

    expect(screen.queryByTestId("rename-conversation-input")).toBeNull();
    expect(mocks.rename.mutate).not.toHaveBeenCalled();
  });
});
