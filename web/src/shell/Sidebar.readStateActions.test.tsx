import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversationPage } from "@/test/sidebarMockHelpers";
// Tests for the read-state actions in the sidebar's session menus:
//   1. A row's kebab always offers exactly one read-state action — "Mark as
//      unread" on a read row, "Mark as read" on a row already showing the
//      unread dot (ConversationMenuItems in Sidebar.tsx).
//   2. The bulk-selection bar offers the matching bulk action: "Mark selected
//      as read" while any selected row is unread, "Mark selected as unread"
//      once the whole selection is read (BulkActionBar in Sidebar.tsx).
// The real read-state store backs these tests; only the data hooks are mocked.

import { cleanup, fireEvent, screen, within } from "@testing-library/react";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return conversationHooksMock();
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import {
  isConversationUnseen,
  markConversationUnread,
  resetReadStateForTests,
  seedReadState,
} from "@/hooks/useUnseenConversations";

const useConvMock = vi.mocked(useConversations);

function conv(id: string, title: string): Conversation {
  return {
    id,
    object: "conversation",
    title,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    labels: {},
    // owner absent → the viewer owns it; idle → the unread dot can show.
    permission_level: null,
    status: "idle",
  };
}

const CONV_A = conv("conv_a", "Session Alpha");
const CONV_B = conv("conv_b", "Session Beta");

function mockConversations(conversations: Conversation[]) {
  const withData = conversationPage(conversations);
  useConvMock.mockImplementation(() => withData);
}

function rowFor(title: string): HTMLElement {
  return screen.getByRole("link", { name: new RegExp(title) }).closest("li") as HTMLElement;
}

function unreadDot(row: HTMLElement): Element | null {
  return row.querySelector('[data-testid="session-state-badge"][data-state="unseen"]');
}

function openKebab(row: HTMLElement) {
  // Radix DropdownMenu opens on pointerdown, not click.
  fireEvent.pointerDown(within(row).getByTestId("conversation-actions"), { button: 0 });
}

function enterSelectionModeAndSelect(titles: string[]) {
  fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
  for (const title of titles) {
    fireEvent.click(screen.getByRole("link", { name: new RegExp(title) }));
  }
}

beforeEach(() => {
  resetReadStateForTests();
  // Seed both rows read-as-of-load, releasing the hydration gate.
  seedReadState([
    { id: CONV_A.id, updated_at: CONV_A.updated_at },
    { id: CONV_B.id, updated_at: CONV_B.updated_at },
  ]);
  mockConversations([CONV_A, CONV_B]);
});

afterEach(() => {
  cleanup();
});

describe("row kebab read-state action", () => {
  it("offers Mark as read on an unread row (never no read-state action), clearing the dot", () => {
    markConversationUnread(CONV_A.id, CONV_A.updated_at);
    renderSidebar();

    const row = rowFor("Session Alpha");
    expect(unreadDot(row)).not.toBeNull();

    openKebab(row);
    // Exactly one read-state action: the unread row swaps Mark as unread for
    // Mark as read.
    expect(screen.queryByTestId("mark-unread-conversation")).toBeNull();
    fireEvent.click(screen.getByTestId("mark-read-conversation"));

    expect(unreadDot(row)).toBeNull();
    expect(isConversationUnseen(CONV_A.id, CONV_A.updated_at, "idle")).toBe(false);

    // The read row's menu offers the reverse action again.
    openKebab(row);
    expect(screen.getByTestId("mark-unread-conversation")).toBeInTheDocument();
    expect(screen.queryByTestId("mark-read-conversation")).toBeNull();
  });
});

describe("bulk-selection read-state action", () => {
  it("marks a selection containing unread rows as read and exits selection mode", () => {
    // Mixed selection (one unread, one read) → the bar offers Mark as read.
    markConversationUnread(CONV_A.id, CONV_A.updated_at);
    renderSidebar();

    enterSelectionModeAndSelect(["Session Alpha", "Session Beta"]);
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    expect(screen.queryByTestId("bulk-mark-unread")).toBeNull();

    fireEvent.click(screen.getByTestId("bulk-mark-read"));

    // The action concludes the selection, like Archive / Move / Delete.
    expect(screen.getByRole("button", { name: "Select sessions" })).toBeInTheDocument();
    expect(unreadDot(rowFor("Session Alpha"))).toBeNull();
    expect(unreadDot(rowFor("Session Beta"))).toBeNull();
  });

  it("marks an all-read selection as unread, lighting both dots", () => {
    renderSidebar();

    enterSelectionModeAndSelect(["Session Alpha", "Session Beta"]);
    expect(screen.queryByTestId("bulk-mark-read")).toBeNull();

    fireEvent.click(screen.getByTestId("bulk-mark-unread"));

    expect(screen.getByRole("button", { name: "Select sessions" })).toBeInTheDocument();
    expect(unreadDot(rowFor("Session Alpha"))).not.toBeNull();
    expect(unreadDot(rowFor("Session Beta"))).not.toBeNull();
  });

  it("disables Mark as unread at zero selection", () => {
    renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));

    expect(screen.getByTestId("bulk-mark-unread")).toBeDisabled();
    fireEvent.click(screen.getByRole("link", { name: /Session Alpha/ }));
    expect(screen.getByTestId("bulk-mark-unread")).toBeEnabled();
  });
});
