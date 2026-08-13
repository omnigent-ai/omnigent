// Tests for the optimistic delete-session flow in the sidebar. The
// contract: clicking "Delete" in the confirm dialog fires the mutation,
// closes the dialog, and leaves the row alone — the mutation itself
// splices the row out of the cached lists on the next frame (see
// `useStopAndDeleteConversation`), so the sidebar never shows an
// in-flight status row for a delete the way it does for an archive.
// See ConversationRow.confirmDelete in Sidebar.tsx.

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
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  // Rename/archive are wired on the row but not exercised here; minimal
  // stubs keep the row from crashing on mount.
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkMoveToProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
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

describe("optimistic delete session flow", () => {
  it("closes the confirm dialog immediately on Delete without awaiting the mutation", () => {
    renderSidebar();
    openDeleteDialog();

    const dialog = screen.getByRole("dialog");
    // Confirm the delete dialog is the one open (vs. some other surface).
    expect(within(dialog).getByText("Delete conversation?")).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    // The mutation fired with the row's id and the (unchecked) branch flag.
    // No mutate-level callbacks: the row is spliced out of the cached lists
    // optimistically, which unmounts this component — callbacks on an
    // unmounted observer never fire, so everything lives in the hook or runs
    // synchronously at click time.
    expect(mocks.del.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.del.mutate).toHaveBeenCalledWith({ id: "conv_1", deleteBranch: false });

    // The dialog is gone even though the mutate stub never resolved. If
    // confirmDelete awaited the server before closing (the old blocking
    // behavior), the dialog would still be open here.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows no in-flight status row — the row is removed optimistically instead", () => {
    // The real hook drops the row from the cached list in onMutate, so by
    // the time a delete is pending the row is already gone from the sidebar.
    // A "Deleting…" placeholder would therefore only ever appear when the
    // splice hadn't happened — and would reintroduce the round-trip-long
    // wait that made delete feel slower than archive.
    mocks.del.isPending = true;
    renderSidebar();

    expect(screen.queryByTestId("conversation-deleting")).not.toBeInTheDocument();
    expect(screen.queryByText("Deleting…")).not.toBeInTheDocument();
    // With this mocked hook the cache is never spliced, so the row stays —
    // and it stays fully interactive rather than being swapped for a stub.
    expect(screen.getByRole("link", { name: /My Session/ })).toBeInTheDocument();
  });

  it("keeps the row interactive when a delete fails (no error row)", () => {
    // A failed delete restores the row and reports via a toast (fired by the
    // hook). The row itself must come back as an ordinary, usable row — the
    // old inline error/retry state can't survive a row that unmounted when
    // it was spliced out.
    mocks.del.isError = true;
    mocks.del.variables = { id: "conv_1", deleteBranch: true };
    renderSidebar();

    expect(screen.queryByTestId("conversation-delete-failed")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /My Session/ })).toBeInTheDocument();
    // No automatic replay: the restored row is the retry affordance.
    expect(mocks.del.mutate).not.toHaveBeenCalled();
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

// --- Active-session redirect on delete --------------------------------
//
// The row leaves the sidebar the moment Delete is confirmed, so the
// redirect runs then too: if the user is viewing the session being
// deleted, the chat surface would otherwise sit on an id that's about to
// 404. Deleting any other row must leave them where they are. These tests
// render with a real router (so useParams resolves the active id) and a
// probe that reports the current pathname.

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

/** Confirm a Delete from `row`'s kebab menu. */
function fireDeleteFrom(row: HTMLElement) {
  fireEvent.pointerDown(within(row).getByTestId("conversation-actions"), { button: 0 });
  fireEvent.click(screen.getByTestId("delete-conversation"));
  act(() => {
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
  });
}

describe("active-session redirect on delete", () => {
  it("redirects to / when the deleted conversation is the active one", () => {
    mockConversations([CONV, CONV_OTHER]);
    renderSidebarRouted("/c/conv_1");
    expect(screen.getByTestId("loc")).toHaveTextContent("/c/conv_1");

    fireDeleteFrom(screen.getByRole("link", { name: /My Session/ }).closest("li") as HTMLElement);

    // Viewing conv_1 when it was deleted → bounce to / immediately, so the
    // chat surface never sits on an id the server is about to forget. The
    // mutate stub never resolves, proving the redirect doesn't wait on it.
    expect(screen.getByTestId("loc")).toHaveTextContent("/");
  });

  it("does NOT redirect when deleting a row the user isn't viewing", () => {
    mockConversations([CONV, CONV_OTHER]);
    renderSidebarRouted("/c/conv_2");
    expect(screen.getByTestId("loc")).toHaveTextContent("/c/conv_2");

    // Delete the *other* session from the sidebar while viewing conv_2.
    fireDeleteFrom(screen.getByRole("link", { name: /My Session/ }).closest("li") as HTMLElement);

    // conv_2 is untouched, so the user stays in the chat they're reading —
    // a redirect here would yank them out of an unrelated session.
    expect(screen.getByTestId("loc")).toHaveTextContent("/c/conv_2");
  });
});
