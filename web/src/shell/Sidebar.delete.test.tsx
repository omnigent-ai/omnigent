// Tests for the async (non-blocking) delete-session flow in the
// sidebar. The contract: clicking "Delete" in the confirm dialog fires
// the mutation and closes the dialog *immediately* (without awaiting the
// server), and the row then shows its own "Deleting…" / error status so
// the user is never blocked on the page. See ConversationRow.confirmDelete
// and DeletingRow in Sidebar.tsx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

// Controllable delete mutation, declared via vi.hoisted so the vi.mock
// factory (hoisted above imports) can reference it. Tests flip
// isPending/isError/variables between renders to drive the row's state.
const mocks = vi.hoisted(() => ({
  del: {
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
    variables: undefined as { id: string; deleteBranch?: boolean } | undefined,
  },
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => mocks.del,
  usePinnedConversationBackfill: () => [],
  // Rename/archive are wired on the row but not exercised here; minimal
  // stubs keep the row from crashing on mount.
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [] }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

// Heavy sibling widgets in the sidebar pull their own hooks/providers;
// stub them so this test stays scoped to the conversation row.
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

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
  permission_level: null, // owner → Delete enabled
  status: "idle",
  git_branch: "feat/login",
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
  // The sidebar fetches a single undifferentiated session list.
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

/** Open the row's action dropdown and click the "Delete" menu item. */
function openDeleteDialog() {
  // Radix DropdownMenu opens on pointerdown, not click.
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
  fireEvent.click(screen.getByTestId("delete-conversation"));
}

beforeEach(() => {
  mocks.del.mutate.mockReset();
  mocks.del.reset.mockReset();
  mocks.del.isPending = false;
  mocks.del.isError = false;
  mocks.del.variables = undefined;
  mockConversations([CONV]);
});

afterEach(() => {
  cleanup();
});

describe("async delete session flow", () => {
  it("closes the confirm dialog immediately on Delete without awaiting the mutation", () => {
    renderSidebar();
    openDeleteDialog();

    const dialog = screen.getByRole("dialog");
    // Confirm the delete dialog is the one open (vs. some other surface).
    expect(within(dialog).getByText("Delete conversation?")).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    // The mutation fired with the row's id and the (unchecked) branch flag,
    // plus an onSuccess callback for the active-session redirect.
    expect(mocks.del.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.del.mutate).toHaveBeenCalledWith(
      { id: "conv_1", deleteBranch: false },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );

    // The dialog is gone even though the mutate stub never invoked
    // onSuccess. If confirmDelete awaited the server before closing
    // (the old blocking behavior), the dialog would still be open here.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders a Deleting… status row while the delete is in flight", () => {
    mocks.del.isPending = true;
    renderSidebar();

    // The in-flight status row replaces the interactive row.
    expect(screen.getByTestId("conversation-deleting")).toBeInTheDocument();
    expect(screen.getByText("Deleting…")).toBeInTheDocument();
    // The navigable row link is gone while deleting — the user can't
    // re-open or re-delete a row that's mid-deletion.
    expect(screen.queryByRole("link", { name: /My Session/ })).not.toBeInTheDocument();
  });

  it("shows a retryable error row when the delete fails; Retry replays the same args", () => {
    mocks.del.isError = true;
    // Mirrors react-query exposing the last mutate() args; a user who
    // opted into branch deletion must get the same on retry.
    mocks.del.variables = { id: "conv_1", deleteBranch: true };
    renderSidebar();

    const failed = screen.getByTestId("conversation-delete-failed");
    expect(within(failed).getByText(/Couldn't delete/)).toBeInTheDocument();
    // The session label is in the *visible* text (not just a tooltip) so
    // the user can tell which row failed when several deletes fail.
    expect(within(failed).getByText("My Session")).toBeInTheDocument();

    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    // Retry replays the exact prior args (incl. deleteBranch: true), not
    // a freshly-recomputed payload that could drop the branch flag.
    expect(mocks.del.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.del.mutate).toHaveBeenCalledWith(
      { id: "conv_1", deleteBranch: true },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it("clears the error row via Dismiss (resets the mutation back to idle)", () => {
    mocks.del.isError = true;
    mocks.del.variables = { id: "conv_1", deleteBranch: false };
    renderSidebar();

    const failed = screen.getByTestId("conversation-delete-failed");
    fireEvent.click(within(failed).getByRole("button", { name: /dismiss delete error/i }));
    // Dismiss calls reset() so the next render drops back to the normal
    // interactive row (isError → false).
    expect(mocks.del.reset).toHaveBeenCalledTimes(1);
  });
});

// --- Branch-name wrapping in the delete dialog ------------------------
//
// A long worktree branch name is a single unbreakable token (no spaces,
// and underscores/hyphens aren't CSS break opportunities). Without an
// explicit break rule + a shrinkable flex column, it overflows the
// rounded "clean up the git worktree" box. These tests lock in the
// classes that fix that: `break-all` on the <code> so the token wraps,
// and `min-w-0` on the text column so the flex item can shrink below
// its content width. Reverting either reintroduces the overflow.

describe("branch-name wrapping in the delete dialog", () => {
  const LONG_BRANCH = "fix_up_down_button_fetching_more_messages";

  it("renders the long branch name with break-all inside a shrinkable column", () => {
    mockConversations([{ ...CONV, git_branch: LONG_BRANCH }]);
    renderSidebar();
    openDeleteDialog();

    const dialog = screen.getByRole("dialog");
    // The <code> element's text is exactly the branch name.
    const code = within(dialog).getByText(LONG_BRANCH);

    // break-all forces the token to wrap at the box edge; without it the
    // unbreakable name spills past the right border (the reported bug).
    expect(code).toHaveClass("break-all");
    // The enclosing text column must be able to shrink below its content
    // width, or the flex item stays at max-content and pushes the <code>
    // out regardless of the break rule.
    expect(code.closest("span")).toHaveClass("min-w-0");
  });

  it("omits the branch-deletion box for a non-worktree conversation", () => {
    // git_branch: null → not a worktree session, so the optional cleanup
    // box (and its checkbox) must not render at all.
    mockConversations([{ ...CONV, git_branch: null }]);
    renderSidebar();
    openDeleteDialog();

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByTestId("delete-branch-checkbox")).not.toBeInTheDocument();
  });
});

// --- Active-session redirect on successful delete ---------------------
//
// Delete is fire-and-forget, so the onSuccess redirect must key off the
// conversation the user is viewing *when it resolves*, not the one they
// were viewing when they clicked Delete. These tests render with a real
// router (so useParams resolves the active id) and a probe that reports
// the current pathname.

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

/** Drive a Delete through to the captured onSuccess callback for `row`. */
function fireDeleteFrom(row: HTMLElement) {
  fireEvent.pointerDown(within(row).getByTestId("conversation-actions"), { button: 0 });
  fireEvent.click(screen.getByTestId("delete-conversation"));
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
  // del.mutate is a stub; pull the onSuccess it was handed so the test
  // can fire it at the moment of its choosing.
  const call = mocks.del.mutate.mock.calls.at(-1);
  return call?.[1]?.onSuccess as () => void;
}

describe("active-session redirect on successful delete", () => {
  it("redirects to / when the deleted conversation is still the active one", () => {
    mockConversations([CONV, CONV_OTHER]);
    renderSidebarRouted("/c/conv_1");
    expect(screen.getByTestId("loc")).toHaveTextContent("/c/conv_1");

    const row = screen.getByRole("link", { name: /My Session/ }).closest("li") as HTMLElement;
    const onSuccess = fireDeleteFrom(row);
    act(() => onSuccess());

    // Still viewing conv_1 when it was deleted → bounce to / so the chat
    // surface doesn't 404 on the missing id.
    expect(screen.getByTestId("loc")).toHaveTextContent("/");
  });

  it("does NOT redirect when the user navigated away before the delete resolved", () => {
    mockConversations([CONV, CONV_OTHER]);
    renderSidebarRouted("/c/conv_1");

    const row = screen.getByRole("link", { name: /My Session/ }).closest("li") as HTMLElement;
    // Initiate the delete while conv_1 is active...
    const onSuccess = fireDeleteFrom(row);
    // ...then navigate to conv_2 before the (still-pending) delete resolves.
    fireEvent.click(screen.getByRole("link", { name: /Other Session/ }));
    expect(screen.getByTestId("loc")).toHaveTextContent("/c/conv_2");

    act(() => onSuccess());

    // The redirect reads the *live* active id (conv_2 ≠ conv_1), so the
    // user stays put. The old code captured isActive=true at click time
    // and would have yanked them to /.
    expect(screen.getByTestId("loc")).toHaveTextContent("/c/conv_2");
  });
});
