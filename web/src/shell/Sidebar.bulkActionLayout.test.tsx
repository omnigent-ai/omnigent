import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversationPage } from "@/test/sidebarMockHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

// Layout regression tests for the sidebar's bulk-action bar (selection
// mode). The bar is a single bordered pill rendered under the Sessions
// header: an inline Exit (X) button, the "N selected" count, and the
// icon-only bulk actions grouped at the trailing edge. It lives
// entirely in normal flow (no absolutely-positioned control, no
// breakpoint-gated duplicate), which is what kept an earlier mobile-overflow
// bug from recurring. These tests lock that structure in:
//   1. The whole bar is in normal flow (no `absolute`), so nothing floats
//      over its neighbours at any breakpoint.
//   2. The actions render exactly once (no mobile/desktop duplication).
//   3. Exit / count / actions share the one pill row.

import { cleanup, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return conversationHooksMock();
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";

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
  const withData = conversationPage(conversations);
  useConvMock.mockImplementation(() => withData);
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
  it("groups Exit, count, and the Archive/Delete actions in one pill, no floating control", () => {
    renderSidebar();
    enterSelectionModeAndSelect();

    const exitBtn = screen.getByRole("button", { name: "Exit selection mode" });
    const deleteBtn = screen.getByTestId("bulk-delete");
    const archiveBtn = screen.getByTestId("bulk-archive");

    // Archive and Delete share the trailing action group.
    const actionGroup = deleteBtn.parentElement as HTMLElement;
    expect(archiveBtn.parentElement).toBe(actionGroup);

    // Exit, count, and the action group all live in the single pill row —
    // and nothing is absolutely positioned, so nothing can float over its
    // neighbours (the mobile-overflow bug this guards against).
    const pill = actionGroup.parentElement as HTMLElement;
    expect(pill).toContainElement(exitBtn);
    expect(pill).toHaveClass("bg-transparent");
    expect(pill).not.toHaveClass("bg-background");
    for (const el of [exitBtn, deleteBtn, archiveBtn, actionGroup, pill]) {
      expect(el.className).not.toMatch(/\babsolute\b/);
    }
  });

  it("keeps the bar visible at every breakpoint and in normal flow", () => {
    renderSidebar();
    enterSelectionModeAndSelect();

    const pill = (screen.getByTestId("bulk-delete").parentElement as HTMLElement)
      .parentElement as HTMLElement;

    // Not breakpoint-gated and not floated, so it renders identically on
    // mobile and desktop without overlapping neighbours.
    expect(pill.className).not.toMatch(/\bhidden\b/);
    expect(pill.className).not.toMatch(/\bmd:hidden\b/);
    expect(pill.className).not.toMatch(/\babsolute\b/);
  });

  it("renders the Archive and Delete actions exactly once (no mobile/desktop duplication)", () => {
    renderSidebar();
    enterSelectionModeAndSelect();

    // The actions are icon-only buttons labelled for assistive tech; there
    // must be a single instance of each (no breakpoint-duplicated copies).
    // With a single owned selection the Delete label carries no count ("Delete").
    expect(screen.getAllByRole("button", { name: "Archive selected" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(1);
  });

  it.each([
    ["session-filter", "Filter sessions"],
    ["toggle-selection-mode", "Exit selection"],
    ["bulk-mark-unread", "Mark as unread"],
    ["bulk-archive", "Archive"],
    ["bulk-move-to-project", "Move to project"],
    ["bulk-delete", "Delete"],
  ])("shows the %s tooltip on hover", async (testId, label) => {
    const user = userEvent.setup();
    renderSidebar();
    enterSelectionModeAndSelect();

    const trigger = screen.getByTestId(testId);
    if (testId === "session-filter" || testId === "bulk-move-to-project") {
      expect(trigger).toHaveAttribute("data-slot", "dropdown-menu-trigger");
      expect(trigger.parentElement).toHaveAttribute("data-slot", "tooltip-trigger");
    }

    await user.hover(trigger);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(label);
  });

  it("shows Archive and Delete disabled at zero selection, enabling them once a row is picked", () => {
    renderSidebar();
    // Enter selection mode WITHOUT selecting anything yet.
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));

    // All three actions are present up front (not conditionally hidden) but
    // disabled while nothing is selected.
    const archiveBtn = screen.getByTestId("bulk-archive");
    const deleteBtn = screen.getByTestId("bulk-delete");
    const moveBtn = screen.getByTestId("bulk-move-to-project");
    expect(archiveBtn).toBeDisabled();
    expect(deleteBtn).toBeDisabled();
    expect(moveBtn).toBeDisabled();
    expect(screen.getByText("0 selected")).toBeInTheDocument();

    // Selecting a row enables all three actions.
    fireEvent.click(screen.getByRole("link", { name: /My Session/ }));
    expect(archiveBtn).toBeEnabled();
    expect(deleteBtn).toBeEnabled();
    expect(moveBtn).toBeEnabled();
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("renders the row checkbox to the LEFT of the title and removes it on exit", () => {
    renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));

    // The checkbox marker sits at the row's leading edge (left-2), not the
    // trailing edge — so the title indents to make room after it.
    const row = screen.getByRole("link", { name: /My Session/ });
    const li = row.closest("li") as HTMLElement;
    const marker = li.querySelector("svg.lucide-square")?.parentElement as HTMLElement;
    expect(marker.className).toMatch(/\bleft-2\b/);
    expect(marker.className).not.toMatch(/\bright-/);

    fireEvent.click(row);
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Exit selection mode" }));

    expect(screen.getByRole("button", { name: "Select sessions" })).toBeInTheDocument();
    expect(li.querySelector("svg.lucide-square")).toBeNull();
    expect(li.querySelector("svg.lucide-square-check")).toBeNull();
    expect(screen.queryByText(/\d+ selected/)).toBeNull();
  });
});
