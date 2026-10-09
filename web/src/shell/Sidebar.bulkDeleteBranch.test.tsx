import { conversationPage } from "@/test/sidebarMockHelpers";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

// Tests for the branch table in the sidebar's bulk-delete modal (selection
// mode). Contract: worktree sessions among the selection each get a table row
// with a checkbox and their local git branch; ticking one opts that session
// into `?delete_branch=true`. Row checkboxes default unchecked (branch deletion
// is irreversible). The header checkbox is tri-state — unchecked when no row is
// ticked, indeterminate ([-]) for a partial selection, checked when all rows
// are ticked — and toggling it flips the whole table at once. The per-session
// branch flags ride along in `bulkDelete.mutate({ ids, deleteBranchIds })`. See
// BulkActionBar in Sidebar.tsx.

import { cleanup, fireEvent, screen, within } from "@testing-library/react";

// Controllable bulk-delete mutation, declared via vi.hoisted so the vi.mock
// factory (hoisted above imports) can reference it and tests can assert the
// payload passed to mutate.
const mocks = vi.hoisted(() => ({
  bulkDelete: { mutate: vi.fn(), isPending: false, isError: false },
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useBulkDeleteConversations: () => mocks.bulkDelete,
  };
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";

const useConvMock = vi.mocked(useConversations);

// Two worktree sessions (git_branch set) and one plain session. Owner
// (owner unset → owned by viewer), idle → selectable + deletable.
const WORKTREE_A: Conversation = {
  id: "conv_a",
  object: "conversation",
  title: "Alpha",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: {},
  permission_level: null,
  status: "idle",
  git_branch: "feat/alpha",
};
const WORKTREE_B: Conversation = {
  ...WORKTREE_A,
  id: "conv_b",
  title: "Beta",
  git_branch: "feat/beta",
};
const PLAIN: Conversation = {
  ...WORKTREE_A,
  id: "conv_c",
  title: "Gamma",
  git_branch: null,
};

function mockConversations(conversations: Conversation[]) {
  const result = conversationPage(conversations);
  useConvMock.mockImplementation(() => result);
}

/** Enter selection mode and select every row, then open the delete modal. */
function selectAllAndOpenDeleteDialog() {
  fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
  fireEvent.click(screen.getByRole("link", { name: /Alpha/ }));
  fireEvent.click(screen.getByRole("link", { name: /Beta/ }));
  fireEvent.click(screen.getByRole("link", { name: /Gamma/ }));
  fireEvent.click(screen.getByTestId("bulk-delete"));
  return screen.getByRole("dialog");
}

beforeEach(() => {
  mocks.bulkDelete.mutate.mockReset();
  mockConversations([WORKTREE_A, WORKTREE_B, PLAIN]);
});

afterEach(() => {
  cleanup();
});

describe("bulk-delete branch list", () => {
  it("lists a branch checkbox only for the selected worktree sessions", () => {
    renderSidebar();
    const dialog = selectAllAndOpenDeleteDialog();

    // Two worktree sessions selected → two checkboxes; the plain session
    // (git_branch null) contributes none.
    expect(within(dialog).getAllByTestId("bulk-delete-branch-checkbox")).toHaveLength(2);
    expect(within(dialog).getByText("feat/alpha")).toBeInTheDocument();
    expect(within(dialog).getByText("feat/beta")).toBeInTheDocument();
    expect(within(dialog).queryByText("feat/gamma")).not.toBeInTheDocument();
  });

  it("defaults every branch unchecked, so a plain confirm deletes no branch", () => {
    renderSidebar();
    const dialog = selectAllAndOpenDeleteDialog();

    for (const box of within(dialog).getAllByTestId("bulk-delete-branch-checkbox")) {
      expect(box).not.toBeChecked();
    }

    fireEvent.click(within(dialog).getByRole("button", { name: /Delete 3 session/ }));

    expect(mocks.bulkDelete.mutate).toHaveBeenCalledWith({
      ids: ["conv_a", "conv_b", "conv_c"],
      deleteBranchIds: new Set(),
    });
  });

  it("threads only the ticked branches into deleteBranchIds", () => {
    renderSidebar();
    const dialog = selectAllAndOpenDeleteDialog();

    // Tick just conv_b's branch.
    fireEvent.click(within(dialog).getAllByTestId("bulk-delete-branch-checkbox")[1]);
    fireEvent.click(within(dialog).getByRole("button", { name: /Delete 3 session/ }));

    expect(mocks.bulkDelete.mutate).toHaveBeenCalledWith({
      ids: ["conv_a", "conv_b", "conv_c"],
      deleteBranchIds: new Set(["conv_b"]),
    });
  });

  it("toggling the header checkbox ticks every branch; the flags reach deleteBranchIds", () => {
    renderSidebar();
    const dialog = selectAllAndOpenDeleteDialog();

    fireEvent.click(within(dialog).getByTestId("bulk-delete-branch-toggle-all"));
    for (const box of within(dialog).getAllByTestId("bulk-delete-branch-checkbox")) {
      expect(box).toBeChecked();
    }

    fireEvent.click(within(dialog).getByRole("button", { name: /Delete 3 session/ }));

    expect(mocks.bulkDelete.mutate).toHaveBeenCalledWith({
      ids: ["conv_a", "conv_b", "conv_c"],
      deleteBranchIds: new Set(["conv_a", "conv_b"]),
    });
  });

  it("re-toggling the header checkbox when all are ticked clears every branch", () => {
    renderSidebar();
    const dialog = selectAllAndOpenDeleteDialog();
    const header = within(dialog).getByTestId("bulk-delete-branch-toggle-all");

    fireEvent.click(header); // none → all
    fireEvent.click(header); // all → none
    for (const box of within(dialog).getAllByTestId("bulk-delete-branch-checkbox")) {
      expect(box).not.toBeChecked();
    }
  });

  it("drives the header checkbox tri-state from the row selection", () => {
    renderSidebar();
    const dialog = selectAllAndOpenDeleteDialog();
    const header = within(dialog).getByTestId("bulk-delete-branch-toggle-all");
    const rows = within(dialog).getAllByTestId("bulk-delete-branch-checkbox");

    // Nothing ticked → unchecked, not indeterminate.
    expect(header).not.toBeChecked();
    expect(header).not.toBePartiallyChecked();

    // One of two ticked → indeterminate ([-]), not checked.
    fireEvent.click(rows[0]);
    expect(header).toBePartiallyChecked();
    expect(header).not.toBeChecked();

    // Both ticked → checked, no longer indeterminate.
    fireEvent.click(rows[1]);
    expect(header).toBeChecked();
    expect(header).not.toBePartiallyChecked();
  });

  it("omits the branch list entirely when no selected session has a worktree", () => {
    mockConversations([PLAIN]);
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
    fireEvent.click(screen.getByRole("link", { name: /Gamma/ }));
    fireEvent.click(screen.getByTestId("bulk-delete"));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByTestId("bulk-delete-branch-checkbox")).not.toBeInTheDocument();
    expect(within(dialog).queryByTestId("bulk-delete-branch-toggle-all")).not.toBeInTheDocument();
  });
});
